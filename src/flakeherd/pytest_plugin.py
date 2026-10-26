"""pytest plugin: quarantine skipping + auto-retry with preserved evidence.

Registered automatically via the ``pytest11`` entry point once
``flakeherd`` is installed - and a no-op when no manifest exists and no
retries are requested, so adding the package to an environment never
changes behavior by accident.

Quarantine
----------

Where the manifest is looked up, in order:

1. ``$FLAKEHERD_MANIFEST`` - explicit path (CI-friendly);
2. ``<rootdir>/.flakeherd/quarantine.json`` - the default location.

Set ``FLAKEHERD_DISABLE=1`` to bypass quarantine temporarily (e.g. while
debugging a quarantined test locally). Quarantined tests are *skipped with
the recorded reason*, never silently dropped, so the cost of flakiness stays
visible in every test run. Entries whose ``expires_at`` date has passed are
treated as lapsed: the test runs again (quarantine must be re-earned).

Auto-retry (``--flakeherd-retries N``)
--------------------------------------

Opt-in: default 0 (off) keeps pytest behavior untouched. With ``N > 0``, a
test whose *setup or call* phase fails is re-run up to ``N`` additional
times. The final attempt decides the recorded outcome, so a test that keeps
failing stays red - a retry can never silently turn a genuine failure into
a green run. Evidence is preserved end to end:

* every attempt is logged normally (console output, per-attempt JUnit
  ``<testcase>`` elements: earlier attempts keep their original
  ``<failure>`` tracebacks, the last one carries the final outcome);
* the final attempt's JUnit element additionally carries
  ``<properties>`` with ``flakeherd_retry_attempts``,
  ``flakeherd_retry_outcomes`` (e.g. ``"F,P"``) and a capped excerpt of
  the first failure (``flakeherd_retry_first_failure``) - the contract
  FlakeHerd's JUnit parser reads back (see ``docs/ARCHITECTURE.md``);
* ``flakeherd record`` classifies fail-then-pass as *suspected flaky*
  evidence, never as a confirmed flake, and never as an excuse for a
  current failure.

Interruptions (KeyboardInterrupt) abort the whole session; the retry loop
never swallows them. Skipped tests (including quarantined ones) are never
retried. Teardown failures are not retried (the test body itself passed).

Compatibility note: do not combine ``--flakeherd-retries`` with
pytest-rerunfailures' ``--reruns`` - both drive the same pytest hook and
one will win unpredictably.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from .errors import ManifestError
from .model import (
    RETRY_PROP_ATTEMPTS,
    RETRY_PROP_FIRST_FAILURE,
    RETRY_PROP_OUTCOMES,
)
from .quarantine import find_match, is_expired, load_manifest
from .quarantine import pytest_id_parts as _pytest_id_parts

# NOTE: pluggy treats any module attr starting with ``pytest_`` as a hook;
# the alias keeps imports out of pluggy's hook discovery.

# Cap for the first-failure excerpt embedded in JUnit properties: the full
# traceback stays in the console output and in the per-attempt <failure>
# elements of the JUnit XML itself.
MAX_EXCERPT_CHARS = 10_000


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


def _manifest_path(config) -> Path | None:
    env = os.environ.get("FLAKEHERD_MANIFEST")
    if env:
        return Path(env)
    return Path(config.rootpath) / ".flakeherd" / "quarantine.json"


def _load_entries(config):
    """Load active (non-expired) entries once per pytest invocation."""
    cached = getattr(config, "_flakeherd_entries", None)
    if cached is not None:
        return cached
    result: tuple[list, list, Path | None] = ([], [], None)
    if os.environ.get("FLAKEHERD_DISABLE", "").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        path = _manifest_path(config)
        if path and path.is_file():
            try:
                entries = load_manifest(path)
            except ManifestError as exc:
                config.issue_config_time_warning(
                    f"flakeherd: ignoring invalid quarantine manifest: {exc}", stacklevel=2
                )
            else:
                expired = [e for e in entries if is_expired(e)]
                active = [e for e in entries if not is_expired(e)]
                result = (active, expired, path)
    config._flakeherd_entries = result
    return result


def pytest_report_header(config):
    """One honest line in the test-run header."""
    entries, expired, path = _load_entries(config)
    if entries:
        suffix = f", {len(expired)} expired" if expired else ""
        return f"flakeherd: {len(entries)} test(s) quarantined{suffix} ({path})"
    if expired:
        return f"flakeherd: {len(expired)} quarantined test(s) expired - running again"
    return None


def pytest_collection_modifyitems(session, config, items):
    entries, _expired, _path = _load_entries(config)
    if not entries:
        return
    remaining: list = []
    for item in items:
        file, classname, name = _pytest_id_parts(item.nodeid)
        entry = find_match(entries, name=name, classname=classname, file=file)
        if entry is None:
            remaining.append(item)
        else:
            item.add_marker(
                pytest.mark.skip(reason=f"flakeherd quarantine [{entry.cls}]: {entry.reason}")
            )
            remaining.append(item)  # keep the item; pytest reports it as SKIPPED
    items[:] = remaining


# ---------------------------------------------------------------------------
# Auto-retry
# ---------------------------------------------------------------------------


@dataclass
class _RetryState:
    """Per-item bookkeeping while the retry protocol runs."""

    max_retries: int
    outcomes: list[str] = field(default_factory=list)
    attempt_failed: bool = False
    first_failure: str = ""

    def properties(self) -> list[tuple[str, str]]:
        props = [
            (RETRY_PROP_ATTEMPTS, str(len(self.outcomes))),
            (RETRY_PROP_OUTCOMES, ",".join(self.outcomes)),
        ]
        if self.first_failure:
            props.append((RETRY_PROP_FIRST_FAILURE, self.first_failure))
        return props


def pytest_addoption(parser):
    group = parser.getgroup("flakeherd", "FlakeHerd flaky-test quarantine")
    group.addoption(
        "--flakeherd-retries",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Retry a failed test up to N additional times before accepting "
            "the failure (setup/call failures only; default: no retries). "
            "The final attempt decides the outcome and every attempt's "
            "evidence is preserved in the JUnit XML."
        ),
    )
    parser.addini(
        "flakeherd_retries",
        type="int",
        default=0,
        help="Default for --flakeherd-retries (0 = off).",
    )


def _max_retries(config) -> int:
    option = config.getoption("--flakeherd-retries")
    value = option if option is not None else config.getini("flakeherd_retries")
    if value < 0:
        raise pytest.UsageError("--flakeherd-retries must be >= 0")
    return value


def _excerpt(report) -> str:
    """One capped, single-line-ish excerpt of a failure report."""
    text = str(report.longrepr) if report.longrepr else f"{report.outcome} in {report.when}"
    text = " ".join(text.split())
    if len(text) > MAX_EXCERPT_CHARS:
        text = text[: MAX_EXCERPT_CHARS - 15] + "...[truncated]"
    return text


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    """Run the standard test protocol, retrying failed attempts.

    Returns ``None`` when retries are off so pytest's default protocol runs
    untouched. With retries on, each attempt runs through pytest's own
    ``runtestprotocol`` (reports logged per attempt, so the console and the
    JUnit XML show every attempt), and the loop stops at the first
    non-failed attempt.
    """
    try:
        retries = _max_retries(item.config)
    except pytest.UsageError:
        raise
    if retries <= 0:
        return None

    from _pytest.runner import runtestprotocol

    state = _RetryState(max_retries=retries)
    item._flakeherd_retry = state
    ihook = item.ihook
    ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    try:
        for _attempt in range(retries + 1):
            reports = runtestprotocol(item, log=True, nextitem=nextitem)
            if not _attempt_failed(reports):
                break
            if _attempt >= retries:
                break
    finally:
        # KeyboardInterrupt (and every other exception) propagates; the
        # state stays on the item so reports already emitted keep whatever
        # evidence was attached along the way.
        try:
            del item._flakeherd_retry
        except AttributeError:
            pass
    ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True


def _attempt_failed(reports) -> bool:
    """True when the setup or call phase of an attempt failed.

    Skipped attempts (e.g. a quarantined test) and teardown failures do not
    trigger a retry: the test body either never ran or already passed.
    """
    return any(r.failed and r.when in ("setup", "call") for r in reports)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Track per-attempt outcomes and attach retry evidence to reports.

    Reports snapshot ``item.user_properties`` when they are created, so the
    evidence must be attached to the *teardown report* of each attempt (the
    last report created for that attempt's JUnit testcase element) - and by
    then the attempt's own call outcome is known. Only elements of tests
    that actually retried (>= 2 recorded outcomes) carry the properties.
    """
    outcome = yield
    state = getattr(item, "_flakeherd_retry", None)
    if state is None:
        return
    report = outcome.get_result()
    if report.when in ("setup", "call"):
        if report.failed:
            state.attempt_failed = True
            if not state.first_failure:
                state.first_failure = _excerpt(report)
        return
    if report.when == "teardown":
        if state.attempt_failed:
            state.outcomes.append("F")
        elif report.skipped:
            state.outcomes.append("S")
        else:
            state.outcomes.append("P")
        state.attempt_failed = False
        if len(state.outcomes) > 1:
            # Attach the cumulative evidence to this attempt's teardown
            # report; the final attempt's element ends up with the complete
            # picture (attempts count + every outcome so far).
            report.user_properties.extend(state.properties())
