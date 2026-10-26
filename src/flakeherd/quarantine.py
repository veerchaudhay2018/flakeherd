"""Quarantine manifest: the machine-readable "pen" for proven-flaky tests.

``flakeherd quarantine`` writes a JSON manifest of tests with demonstrated
flakiness. Test runners can then skip those tests while keeping the reason
visible in output (a skipped-with-reason test is honest; a deleted test is
not). The default selection is conservative - only *confirmed-flaky* and
*flaky* classifications enter the pen; low-confidence candidates
(``flaky-low``, ``suspected-flaky``) require an explicit opt-in, and
broken/regressed tests are never quarantined (that would hide real bugs).

Score-threshold policy (opt-in, ``quarantine_threshold_enabled``): when
enabled, a test additionally needs a flakiness score of at least
``quarantine_score_threshold`` (default 40) and at least
``quarantine_min_samples`` recorded outcomes. Every selected entry carries
an ``audit_reason`` - the machine-readable justification of *why* it
qualified - and, when ``quarantine_expiry_days`` > 0, an ``expires_at``
date after which the pytest plugin stops skipping it (a quarantine that
lapses must be re-earned with fresh evidence).

The manifest is stable, diff-able JSON: it is meant to be committed, code
reviewed and reverted exactly like source code. FlakeHerd never commits or
pushes the manifest itself. Schema version 1 is unchanged: the new
``expires_at`` / ``audit_reason`` entry fields are optional, so manifests
written by flakeherd 0.2.0 still load in 0.1.x and vice versa.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import (
    Config,
    effective_quarantine_min_samples,
    effective_quarantine_min_score,
)
from .errors import ManifestError
from .model import (
    DEFAULT_QUARANTINE_CLASSES,
    LOW_CONFIDENCE_CLASSES,
    Classification,
)
from .util import atomic_write_bytes, utc_now_iso

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class QuarantineEntry:
    """One quarantined test, with the evidence that justified the decision."""

    test_key: str
    name: str
    classname: str | None
    file: str | None
    cls: str
    score: int
    failure_rate: float
    samples: int
    reason: str
    # Optional fields (schema version 1, additive): absent in manifests
    # written by flakeherd <= 0.1.1 and defaulted on load.
    expires_at: str | None = None  # ISO date (YYYY-MM-DD); None = never
    audit_reason: str = ""  # why this entry qualified, machine-generated


def select(
    classifications: list[Classification],
    cfg: Config,
    *,
    cli_min_score: int | None = None,
    today: date | None = None,
) -> list[QuarantineEntry]:
    """Pick quarantine candidates from a classification list.

    Ordered by score (most-flaky first), then key, so manifest churn is
    minimal between runs. Only flaky-family classifications can ever be
    selected - a consistently failing (broken) or regressing test is never
    quarantined, regardless of configuration.
    """
    classes = set(DEFAULT_QUARANTINE_CLASSES)
    if cfg.include_low_confidence:
        classes |= LOW_CONFIDENCE_CLASSES
    min_score = effective_quarantine_min_score(cfg, cli_min_score)
    min_samples = effective_quarantine_min_samples(cfg)
    policy_on = cfg.quarantine_threshold_enabled
    expiry_days = cfg.quarantine_expiry_days
    entries: list[QuarantineEntry] = []
    for c in classifications:
        if c.cls not in classes or c.score < min_score:
            continue
        if policy_on and c.samples < min_samples:
            continue  # not enough history for an automated decision
        audit = _audit_reason(c, min_score, min_samples if policy_on else None, policy_on)
        expires_at = None
        if expiry_days > 0:
            base = today or date.today()
            expires_at = (base + timedelta(days=expiry_days)).isoformat()
        entries.append(
            QuarantineEntry(
                test_key=c.test_key,
                name=c.name,
                classname=c.classname,
                file=c.file,
                cls=c.cls,
                score=c.score,
                failure_rate=c.failure_rate,
                samples=c.samples,
                reason=c.reason,
                expires_at=expires_at,
                audit_reason=audit,
            )
        )
    entries.sort(key=lambda e: (-e.score, e.test_key))
    return entries


def _audit_reason(
    c: Classification,
    min_score: int,
    min_samples: int | None,
    policy_on: bool,
) -> str:
    """Machine-readable justification recorded with every new entry."""
    parts = [f"score {c.score} >= {min_score}", f"class {c.cls}"]
    if policy_on and min_samples is not None:
        parts.append(f"samples {c.samples} >= {min_samples}")
        parts.append("policy=threshold")
    else:
        parts.append("policy=legacy")
    return "; ".join(parts)


def is_expired(entry: QuarantineEntry, *, today: date | None = None) -> bool:
    """True when *entry* carries an ``expires_at`` date that has passed.

    Entries without an expiry date never expire. Comparison is by calendar
    date; an entry expiring today is still active (it lapses the next day).
    """
    if not entry.expires_at:
        return False
    try:
        expires = date.fromisoformat(entry.expires_at)
    except ValueError:
        # A malformed date must never silently un-quarantine a test.
        return False
    today = today or date.today()
    return today > expires


def write_manifest(
    path: str | Path,
    entries: list[QuarantineEntry],
    *,
    generator: str,
) -> None:
    """Write the manifest atomically as stable, diff-friendly JSON."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generator": generator,
        "generated_at": utc_now_iso(),
        "quarantined": [asdict(e) for e in entries],
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    try:
        atomic_write_bytes(path, (body + "\n").encode("utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot write manifest {path}: {exc}") from exc


def load_manifest(path: str | Path) -> list[QuarantineEntry]:
    """Read and validate a manifest. Raises :class:`ManifestError` if invalid."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {path}: invalid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest {path}: top-level value must be an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(
            f"manifest {path}: unsupported schema_version {raw.get('schema_version')!r} "
            f"(expected {SCHEMA_VERSION})"
        )
    rows = raw.get("quarantined")
    if not isinstance(rows, list):
        raise ManifestError(f"manifest {path}: 'quarantined' must be a list")
    entries: list[QuarantineEntry] = []
    required = {"test_key", "name", "cls", "reason"}
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or not required.issubset(row):
            raise ManifestError(
                f"manifest {path}: entry #{i} is missing one of {sorted(required)}"
            )
        try:
            entries.append(
                QuarantineEntry(
                    test_key=str(row["test_key"]),
                    name=str(row["name"]),
                    classname=row.get("classname"),
                    file=row.get("file"),
                    cls=str(row["cls"]),
                    score=int(row.get("score", 0)),
                    failure_rate=float(row.get("failure_rate", 0.0)),
                    samples=int(row.get("samples", 0)),
                    reason=str(row["reason"]),
                    # Optional since 0.2.0 (schema_version stays 1): absent in
                    # manifests written by flakeherd <= 0.1.1.
                    expires_at=row.get("expires_at"),
                    audit_reason=str(row.get("audit_reason", "")),
                )
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ManifestError(
                f"manifest {path}: invalid field value in entry #{i}: {exc}"
            ) from exc

    return entries


# ---------------------------------------------------------------------------
# Matching: manifest entries -> pytest items
# ---------------------------------------------------------------------------


def strip_params(name: str) -> str:
    """``test_x[param]`` -> ``test_x`` (JUnit/pytest parameter suffixes)."""
    return name.split("[", 1)[0]


def pytest_id_parts(nodeid: str) -> tuple[str, str | None, str]:
    """Split a pytest nodeid into (file path, junit-style classname, name).

    ``tests/unit/test_a.py::TestFoo::test_x[p]`` becomes
    ``("tests/unit/test_a.py", "tests.unit.test_a.TestFoo", "test_x[p]")``
    - mirroring how pytest itself derives ``classname`` from the
    rootdir-relative file path (separators become dots, ``.py`` dropped), so
    matching a manifest against live collection is symmetric.
    """
    parts = nodeid.split("::")
    file = parts[0]
    name = parts[-1]
    class_parts = parts[1:-1]
    classname: str | None = None
    if file.endswith(".py"):
        module = file[:-3].replace("/", ".").replace("\\", ".")
        classname = ".".join([module, *class_parts]) if class_parts else module
    elif class_parts:
        classname = ".".join(class_parts)
    return file, classname, name


def find_match(
    entries: list[QuarantineEntry],
    *,
    name: str,
    classname: str | None,
    file: str,
) -> QuarantineEntry | None:
    """Return the entry matching a live test item, or None.

    Matching is deliberately conservative to avoid quarantining the wrong
    test in a large suite. The name must match (parameter suffixes stripped
    on both sides), AND every identity field the entry provides must agree:

    * ``classname`` must equal the live classname (or be its suffix);
    * ``file`` must match the live file (path or basename).

    An entry carrying neither classname nor file may match by name only
    when it is the *unambiguous* single entry for that name - if any other
    entry with the same name carries identity, or several identity-less
    entries share a name, matching is refused rather than guessed.
    """
    base = strip_params(name)
    candidates = [e for e in entries if strip_params(e.name) == base]
    if not candidates:
        return None
    if len(candidates) == 1 and candidates[0].classname is None and candidates[0].file is None:
        return candidates[0]
    for entry in candidates:
        if entry.classname is None and entry.file is None:
            continue  # ambiguous identity-less entry - never guess
        if entry.classname is not None:
            if not classname or not (
                classname == entry.classname
                or classname.endswith("." + entry.classname)
            ):
                continue
        if entry.file is not None and not _file_matches(entry.file, file):
            continue
        return entry
    return None


def _file_matches(entry_file: str, item_file: str) -> bool:
    entry_path = Path(entry_file)
    item_path = Path(item_file)

    if entry_path == item_path:
        return True

    # A basename fallback is useful when one source provides only a filename,
    # but must not match two different directory-qualified paths.
    entry_has_dir = entry_path.parent != Path(".")
    item_has_dir = item_path.parent != Path(".")

    return (
        entry_path.name == item_path.name
        and not (entry_has_dir and item_has_dir)
    )
