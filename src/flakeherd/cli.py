"""The ``flakeherd`` command-line interface.

Subcommands
-----------
* ``record``    - parse JUnit XML files, store outcomes, report, gate.
* ``report``    - analyze existing history without recording.
* ``quarantine``- write the skip manifest for proven-flaky tests.
* ``ci-sync``   - pull/push history through the repository's cache branch.
* ``health``    - compute the Test Health metric and render the SVG badge.
* ``init``      - write an example ``.flakeherd.json``.

Exit codes: 0 = ok, 1 = gate triggered (--fail-on), 2 = usage/data error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .ci_sync import ci_pull, ci_push
from .config import CONFIG_FILENAME, Config, with_overrides
from .detector import analyze
from .errors import FlakeHerdError
from .health import (
    DEFAULT_MIN_RUNS,
    compute_health,
    load_report_payload,
    render_badge_svg,
    write_badge,
)
from .junit import discover, parse_files
from .model import CLASS_LABELS, RunInfo
from .quarantine import select as quarantine_select
from .quarantine import write_manifest
from .report import (
    append_step_summary,
    build_payload,
    emit_annotations,
    in_github_actions,
    render,
    summary_markdown,
    write_json,
)
from .report.terminal import should_use_color
from .store import Store
from .util import new_run_uid

DEFAULT_DB = ".flakeherd/history.db"
DEFAULT_HISTORY_BRANCH = "flakeherd/history"
DEFAULT_JUNIT_GLOB = "**/junit*.xml"
DEFAULT_BADGE = "test-health.svg"


def _info(msg: str) -> None:
    print(f"flakeherd: {msg}", file=sys.stderr)


def _db_path(args) -> str:
    return getattr(args, "db", None) or os.environ.get("FLAKEHERD_DB") or DEFAULT_DB


def _load_config(args) -> Config:
    cfg_path = getattr(args, "config", None) or (Path.cwd() / CONFIG_FILENAME)
    cfg = Config.load(cfg_path)
    window = getattr(args, "window", None)
    overrides: dict = {"window": window} if window is not None else {}
    if getattr(args, "max_annotations", None) is not None:
        overrides["max_annotations"] = args.max_annotations
    return with_overrides(cfg, **overrides) if overrides else cfg


def _git_stdout(*args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _detect_sha(explicit: str | None, use_git: bool) -> str:
    if explicit:
        return explicit
    env_sha = os.environ.get("GITHUB_SHA")
    if env_sha:
        return env_sha
    if use_git:
        return _git_stdout("rev-parse", "HEAD") or "unknown"
    return "unknown"


def _detect_branch(explicit: str | None, use_git: bool) -> str:
    if explicit:
        return explicit
    env_branch = os.environ.get("GITHUB_REF_NAME")
    if env_branch:
        return env_branch
    if use_git:
        return _git_stdout("symbolic-ref", "--short", "HEAD") or "unknown"
    return "unknown"


def _detect_run_uid(explicit: str | None) -> str:
    if explicit:
        return explicit
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        return f"{run_id}-{attempt}"
    return new_run_uid("local")


def _github_mode(args) -> str:
    return getattr(args, "github", "auto")


def _emit_github(result, args, cfg) -> None:
    mode = _github_mode(args)
    active = in_github_actions() if mode == "auto" else mode == "on"
    if not active:
        return
    count = emit_annotations(result, cfg.max_annotations)
    if count:
        _info(f"emitted {count} GitHub annotation(s)")
    if append_step_summary(summary_markdown(result)):
        _info("appended job summary to $GITHUB_STEP_SUMMARY")


def _print_report(
    result,
    db: str,
    cfg: Config,
    args,
    *,
    current_run_results=None,
    quarantine_suggestions=None,
) -> None:
    json_out = getattr(args, "json_out", None)
    if json_out:
        payload = build_payload(
            result,
            window=cfg.window,
            current_run_results=current_run_results,
            quarantine_suggestions=quarantine_suggestions,
        )
        write_json(json_out, payload)
        _info(f"wrote JSON report -> {json_out}")
    _emit_github(result, args, cfg)
    if getattr(args, "no_report", False):
        return
    text = render(
        result,
        db_path=db,
        use_color=should_use_color(),
        quarantine_suggestions=quarantine_suggestions,
    )
    print(text)


def _gate_exit(result, args) -> int:
    if getattr(args, "fail_on", "never") != "unexplained":
        return 0
    gate = result.gate_failures()
    if gate:
        _info(
            f"gate: {len(gate)} unexplained failure(s) "
            f"(failing tests without flaky evidence)"
        )
        return 1
    return 0


def _open_store_or_fail(db: str, *, must_exist: bool) -> Store:
    if must_exist and not Path(db).exists():
        raise FlakeHerdError(
            f"no history database at {db} yet - run `flakeherd record` first"
        )
    return Store(db)


# ---------------------------------------------------------------------------
# subcommand implementations
# ---------------------------------------------------------------------------


def _cmd_record(args) -> int:
    cfg = _load_config(args)
    patterns = args.junit or [DEFAULT_JUNIT_GLOB]
    files = discover(patterns)
    if not files:
        raise FlakeHerdError(
            f"no JUnit XML files matched {patterns}; pass --junit GLOB "
            "(e.g. --junit 'test-results/*.xml')"
        )
    parsed = parse_files(files)

    run_uid = _detect_run_uid(args.run_uid)
    use_git = not args.no_git_defaults
    run = RunInfo(
        run_uid=run_uid,
        commit_sha=_detect_sha(args.sha, use_git),
        branch=_detect_branch(args.branch, use_git),
    )

    db = _db_path(args)
    with _open_store_or_fail(db, must_exist=False) as store:
        stats = store.record_run(run, parsed.results.values())
        if not args.no_prune:
            store.prune(cfg.max_runs)
        # The gate must judge the run that was just recorded, never a merged
        # remote run with a newer/skewed timestamp.
        result = analyze(store, cfg, current_run_uid=run.run_uid)

    _info(
        f"recorded {stats.tests_recorded} test(s) from {len(files)} file(s) "
        f"-> {db} (run {run_uid}, sha {run.commit_sha[:12]})"
    )
    if parsed.warnings:
        for warning in parsed.warnings[:5]:
            _info(f"warning: {warning}")
        if len(parsed.warnings) > 5:
            _info(f"... {len(parsed.warnings) - 5} more parser warning(s) suppressed")

    # Quarantine policy: suggestion mode by default; --auto-quarantine is
    # the explicit opt-in for writing the manifest during record.
    suggestions = None
    if cfg.quarantine_threshold_enabled:
        suggestions = quarantine_select(result.classifications, cfg)
    if args.auto_quarantine:
        if not cfg.quarantine_threshold_enabled:
            raise FlakeHerdError(
                "--auto-quarantine requires the quarantine policy - set "
                "'quarantine_threshold_enabled': true in .flakeherd.json "
                "first (the policy enforces the score threshold and the "
                "minimum-sample guard before anything is written)"
            )
        out = args.quarantine_out or ".flakeherd/quarantine.json"
        write_manifest(out, suggestions or [], generator=f"flakeherd {__version__}")
        count = len(suggestions or [])
        _info(
            f"--auto-quarantine: wrote {count} quarantined test(s) -> {out} "
            "(the manifest is NOT committed or pushed automatically)"
        )

    _print_report(
        result,
        db,
        cfg,
        args,
        current_run_results=parsed.results,
        quarantine_suggestions=suggestions,
    )
    return _gate_exit(result, args)


def _cmd_report(args) -> int:
    cfg = _load_config(args)
    db = _db_path(args)
    with _open_store_or_fail(db, must_exist=True) as store:
        result = analyze(store, cfg, current_run_uid=args.run_uid)
    suggestions = None
    if cfg.quarantine_threshold_enabled:
        suggestions = quarantine_select(result.classifications, cfg)
    _print_report(result, db, cfg, args, quarantine_suggestions=suggestions)
    return _gate_exit(result, args)


def _cmd_quarantine(args) -> int:
    cfg = _load_config(args)
    overrides: dict = {}
    if args.min_score is not None:
        overrides["quarantine_min_score"] = args.min_score
    if args.include_low_confidence:
        overrides["include_low_confidence"] = True
    if args.min_samples is not None:
        overrides["quarantine_min_samples"] = args.min_samples
    if args.expiry_days is not None:
        overrides["quarantine_expiry_days"] = args.expiry_days
    if overrides:
        cfg = with_overrides(cfg, **overrides)

    db = _db_path(args)
    with _open_store_or_fail(db, must_exist=True) as store:
        result = analyze(store, cfg, current_run_uid=args.run_uid)
    entries = quarantine_select(
        result.classifications, cfg, cli_min_score=args.min_score
    )

    if args.dry_run:
        if not entries:
            print("nothing qualifies for quarantine")
        for e in entries:
            print(f"{e.test_key}  [{e.cls}, score {e.score}]  {e.reason}")
            if e.audit_reason:
                print(f"  audit: {e.audit_reason}")
            if e.expires_at:
                print(f"  expires: {e.expires_at}")
        return 0

    out = args.out or ".flakeherd/quarantine.json"
    write_manifest(out, entries, generator=f"flakeherd {__version__}")
    if entries:
        print(f"wrote {len(entries)} quarantined test(s) -> {out}")
        for e in entries:
            print(f"  - {e.test_key}  [{CLASS_LABELS.get(e.cls, e.cls)}, score {e.score}]")
            if e.audit_reason:
                print(f"      audit: {e.audit_reason}")
            if e.expires_at:
                print(f"      expires: {e.expires_at}")
        print(
            "pytest picks up .flakeherd/quarantine.json automatically once "
            "flakeherd is installed; other runners can read the same manifest."
        )
        print(
            "the manifest is a file in your working tree - committing it is "
            "your decision (flakeherd never runs git write commands for it)."
        )
    else:
        print(f"nothing qualifies for quarantine - wrote empty manifest -> {out}")
    return 0


def _cmd_ci_sync(args) -> int:
    db = _db_path(args)
    branch = args.history_branch
    if args.mode == "pull":
        res = ci_pull(db, remote=args.remote, branch=branch)
        if res.status == "no-history":
            _info(f"no history branch on {args.remote} yet - starting fresh")
        elif res.status == "empty-history":
            _info(f"{args.remote}/{branch} exists but carries no history.db yet")
        else:
            _info(
                f"merged {res.runs_added} run(s), {res.outcomes_added} outcome(s) "
                f"from {args.remote}/{branch}"
            )
        return 0
    res = ci_push(db, remote=args.remote, branch=branch)
    if res.status == "rejected":
        _info(
            f"could not push history to {args.remote}/{branch} "
            "(lost a concurrent-push race twice, or missing write permission) - "
            "this run's rows stay local; analysis is unaffected"
        )
        if res.detail:
            _info(f"git said: {res.detail}")
        return 0
    label = "created" if res.status == "created" else "updated"
    retry = " (after one retry)" if res.retried else ""
    _info(f"{label} {args.remote}/{branch} at {res.commit[:12]}{retry}")
    return 0


def _cmd_health(args) -> int:
    """Compute the Test Health metric from a record --json-out report."""
    from dataclasses import replace

    payload = load_report_payload(args.report)
    health = replace(
        compute_health(payload, min_runs=args.min_runs),
        source_report=str(args.report),
    )
    if args.json_out:
        write_json(args.json_out, health.to_payload())
        _info(f"wrote health summary -> {args.json_out}")
    if args.badge is not None:
        svg = render_badge_svg(health, label=args.label)
        write_badge(args.badge, svg)
        _info(f"wrote health badge -> {args.badge}")

    print("FlakeHerd test health")
    print(f"  state    : {health.state}")
    if health.health_score is None:
        print(
            f"  score    : n/a (insufficient data - {health.runs} run(s) "
            f"recorded, {health.min_runs_required} required)"
        )
    else:
        print(f"  score    : {health.health_score}/100  (heuristic, not a probability)")
    print(
        f"  samples  : {health.runs} run(s), {health.scored_tests} scored "
        f"test(s) of {health.total_tests} total"
    )
    print(f"  metric   : version {health.metric_version}, generated {health.generated_at}")
    if health.distribution:
        summary = ", ".join(f"{cls}={n}" for cls, n in health.distribution.items())
        print(f"  classes  : {summary}")
    if args.badge is not None:
        print(
            f"  badge    : {args.badge} (rendered locally; commit it or fetch "
            "it from CI artifacts - there is no public badge URL)"
        )
    return 0


def _cmd_init(args) -> int:
    path = Path(args.path or CONFIG_FILENAME)
    if path.exists() and not args.force:
        raise FlakeHerdError(f"{path} already exists (use --force to overwrite)")
    example = {
        "window": 30,
        "max_runs": 500,
        "min_regression_streak": 2,
        "max_annotations": 10,
        "quarantine_min_score": 0,
        "include_low_confidence": False,
        "ignore": [],
        # Score-threshold quarantine policy (all opt-in; see README):
        "quarantine_threshold_enabled": False,
        "quarantine_score_threshold": 40,
        "quarantine_min_samples": 5,
        "quarantine_expiry_days": 0,
    }
    path.write_text(json.dumps(example, indent=2) + "\n", encoding="utf-8")
    print(f"wrote example configuration -> {path}")
    print("next steps:")
    print("  1. run your tests with a JUnit XML report (e.g. pytest --junitxml=junit.xml)")
    print("  2. flakeherd record --junit junit.xml")
    print("  3. flakeherd quarantine   # after a few runs")
    print("  4. flakeherd health --report report.json --badge test-health.svg")
    return 0


# ---------------------------------------------------------------------------
# parser wiring
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser, *, db: bool = True) -> None:
    if db:
        p.add_argument("--db", help=f"path to the history database (default {DEFAULT_DB})")
    p.add_argument("--config", help=f"path to config file (default {CONFIG_FILENAME})")


def _add_analysis(p: argparse.ArgumentParser) -> None:
    p.add_argument("--window", type=int, help="override 'window' from the config file")
    p.add_argument("--max-annotations", type=int, help="cap GitHub annotations per level")
    p.add_argument("--json-out", help="also write a machine-readable JSON report to this path")
    p.add_argument(
        "--github",
        choices=["auto", "on", "off"],
        default="auto",
        help="emit ::annotations and step summary (auto = only inside GitHub Actions)",
    )
    p.add_argument(
        "--fail-on",
        choices=["never", "unexplained"],
        default="never",
        help="exit 1 when currently-failing tests have no flaky evidence",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flakeherd",
        description=(
            "History-aware flaky-test detection and quarantine for GitHub Actions. "
            "Record JUnit XML results across runs, classify flakiness from real "
            "history, and quarantine the proven flakes - all inside your repo."
        ),
    )
    parser.add_argument("--version", action="version", version=f"flakeherd {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_record = sub.add_parser("record", help="record a run's JUnit XML results and report")
    p_record.add_argument(
        "-j", "--junit", action="append",
        help=f"glob of JUnit XML file(s), repeatable (default {DEFAULT_JUNIT_GLOB!r})",
    )
    _add_common(p_record)
    p_record.add_argument("--sha", help="commit sha (default $GITHUB_SHA or git HEAD)")
    p_record.add_argument("--branch", help="branch name (default $GITHUB_REF_NAME or git)")
    p_record.add_argument(
        "--run-uid", help="unique run id (default $GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT)"
    )
    p_record.add_argument(
        "--no-git-defaults", action="store_true", help="never consult git for defaults"
    )
    p_record.add_argument(
        "--no-report", action="store_true", help="record without printing a report"
    )
    p_record.add_argument("--no-prune", action="store_true", help="skip pruning to max_runs")
    p_record.add_argument(
        "--auto-quarantine", action="store_true",
        help=(
            "explicit opt-in: also write .flakeherd/quarantine.json for this "
            "run (requires the threshold policy; the manifest is never "
            "committed or pushed automatically)"
        ),
    )
    p_record.add_argument(
        "--quarantine-out",
        help="manifest path for --auto-quarantine (default .flakeherd/quarantine.json)",
    )
    _add_analysis(p_record)
    p_record.set_defaults(func=_cmd_record)

    p_report = sub.add_parser("report", help="analyze recorded history and report")
    _add_common(p_report)
    p_report.add_argument(
        "--run-uid", help="judge this run instead of the newest one (gate/report)"
    )
    _add_analysis(p_report)
    p_report.set_defaults(func=_cmd_report)

    p_quarantine = sub.add_parser("quarantine", help="write a quarantine manifest")
    _add_common(p_quarantine)
    p_quarantine.add_argument(
        "--run-uid", help="judge this run instead of the newest one"
    )
    p_quarantine.add_argument(
        "--out", help="manifest path (default .flakeherd/quarantine.json)"
    )
    p_quarantine.add_argument(
        "--min-score", type=int, help="only quarantine tests scoring at least N"
    )
    p_quarantine.add_argument(
        "--include-low-confidence", action="store_true",
        help="also quarantine 'flaky-low' and 'suspected-flaky' classifications",
    )
    p_quarantine.add_argument(
        "--min-samples", type=int,
        help="require at least N recorded samples (enforced while the "
             "threshold policy is enabled)",
    )
    p_quarantine.add_argument(
        "--expiry-days", type=int,
        help="stamp new entries with an expires_at date N days out "
             "(0 = never expire; expired entries run again)",
    )
    p_quarantine.add_argument(
        "--dry-run", action="store_true", help="print candidates, write nothing"
    )
    p_quarantine.set_defaults(func=_cmd_quarantine)

    p_sync = sub.add_parser("ci-sync", help="sync history through the repository's cache branch")
    _add_common(p_sync)
    p_sync.add_argument("--mode", choices=["pull", "push"], required=True)
    p_sync.add_argument("--remote", default="origin", help="git remote (default origin)")
    p_sync.add_argument(
        "--history-branch", default=DEFAULT_HISTORY_BRANCH,
        help=f"branch holding history.db (default {DEFAULT_HISTORY_BRANCH})",
    )
    p_sync.set_defaults(func=_cmd_ci_sync)

    p_health = sub.add_parser(
        "health",
        help="compute the Test Health metric and render an SVG badge from a "
             "'record --json-out' report",
    )
    p_health.add_argument(
        "--report", required=True,
        help="path to the JSON report produced by 'flakeherd record --json-out'",
    )
    p_health.add_argument(
        "--badge", nargs="?", const=DEFAULT_BADGE, default=None,
        help=f"write the SVG badge locally (default path when flag given: {DEFAULT_BADGE})",
    )
    p_health.add_argument(
        "--json-out", help="also write a machine-readable health summary to this path"
    )
    p_health.add_argument(
        "--min-runs", type=int, default=DEFAULT_MIN_RUNS,
        help=f"runs required before the score is reported (default {DEFAULT_MIN_RUNS})",
    )
    p_health.add_argument(
        "--label", default="tests", help="badge label text (default 'tests')"
    )
    p_health.set_defaults(func=_cmd_health)

    p_init = sub.add_parser("init", help="write an example .flakeherd.json")
    p_init.add_argument("--path", help=f"config path (default {CONFIG_FILENAME})")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing file")
    p_init.set_defaults(func=_cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FlakeHerdError as exc:
        print(f"flakeherd: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("flakeherd: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
