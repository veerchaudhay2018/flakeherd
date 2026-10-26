"""JUnit XML parsing - FlakeHerd's universal input adapter.

JUnit XML is the de-facto interchange format for test results. Reading it
means FlakeHerd works with virtually any test framework out of the box:

* Python: ``pytest --junitxml=...``
* JavaScript/TypeScript: jest via ``jest-junit``, vitest junit reporter
* Go: ``go test -v`` piped through ``go-junit-report``
* Java/Kotlin: Maven Surefire, Gradle
* Elixir, Rust (``cargo-nextest``), Ruby, C# (``JUnitXml.TestLogger``), ...

Design notes / safety:

* Parsing uses ``xml.etree.ElementTree.iterparse``; processed testcases are
  cleared AND detached from their parent, so memory stays bounded on very
  large reports instead of accumulating emptied element shells.
* Files larger than ``max_bytes`` (default 64 MiB) are rejected up front -
  a CI artifact should not be that large, and it protects runners from
  pathological or malicious report files.
* ``.xml.gz`` files are transparently decompressed, and the size cap is
  enforced on the *decompressed* content (compressed size proves nothing:
  a small gzip bomb can expand into gigabytes).
* Duplicate test keys within one run (sharded reports, retries reported as
  separate ``<testcase>`` elements) are merged. Divergent pass/fail
  duplicates keep the *last* element's status (retry producers write
  attempts in temporal order, so the final attempt is last) plus
  ``flaky_hint=True`` for the divergence itself. Known limitation:
  pytest-rerunfailures >= 15 records failed rerun attempts as *bare*
  testcase elements (indistinguishable from passes), so a fail-then-pass
  recovery from that plugin yields two identical-looking pass elements -
  the outcome is correct but no hint can be derived; FlakeHerd's own
  ``--flakeherd-retries`` records explicit per-attempt properties instead.
* Retry evidence contract: a ``<testcase>`` may carry a
  ``<properties><property name="flakeherd_retry_attempts" .../></properties>``
  node (written by FlakeHerd's pytest auto-retry adapter; any other framework
  adapter may emit the same contract). FlakeHerd reads the attempt count, the
  per-attempt outcome string (e.g. ``"F,P"``) and a capped excerpt of the
  first failure. A fail-then-pass recovery additionally sets ``flaky_hint``.
* ``<rerunFailure>`` (pytest-rerunfailures) and ``<flakyFailure>``
  (Maven Surefire) children are recognized explicitly: a ``rerunFailure``
  marks a *failed attempt* (status=fail + hint), a ``flakyFailure`` marks a
  *recovered flake* (Surefire reports those tests as passing - status stays
  pass + hint). Neither may be silently dropped: before 0.2.0 a bare
  ``<rerunFailure>``-only element was parsed as a plain pass, which let a
  test that failed *every* retry attempt be recorded as green.
"""

from __future__ import annotations

import gzip
import io
import os
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatch
from glob import glob
from pathlib import Path

from .errors import ParseError
from .model import (
    RETRY_PROP_ATTEMPTS,
    RETRY_PROP_FIRST_FAILURE,
    RETRY_PROP_OUTCOMES,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    TestCaseResult,
)

MAX_XML_BYTES = 64 * 1024 * 1024

# Capped excerpt of the first failure kept from retry evidence (the full
# traceback stays in the JUnit XML artifact and the console output).
MAX_FIRST_FAILURE_EXCERPT = 10_000

_FAIL_ATTR_VALUES = {"failed", "fail", "failing", "error", "errored", "red"}
_SKIP_ATTR_VALUES = {"skipped", "skip", "ignored", "notrun"}

# Child tags with explicit retry semantics. ``rerunFailure``
# (pytest-rerunfailures) marks a *failed attempt*; ``flakyFailure`` (Maven
# Surefire) marks a *recovered flake* - Surefire reports those tests as
# passing, so the element must not become a failure.
_RERUN_FAILURE_TAGS = {"rerunfailure"}
_FLAKY_FAILURE_TAGS = {"flakyfailure"}


@dataclass
class ParsedReport:
    """Parse output for a single run: merged results plus warnings."""

    results: dict[str, TestCaseResult] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def discover(patterns: Iterable[str], base_dir: str | Path = ".") -> list[Path]:
    """Expand glob patterns into a deduplicated, sorted list of files.

    Patterns are evaluated relative to *base_dir* and support ``**``
    (recursive matching) via :mod:`glob`.
    """
    base = Path(base_dir)
    found: dict[str, Path] = {}
    for pattern in patterns:
        for entry in sorted(glob(str(base / pattern), recursive=True)):
            if os.path.isfile(entry):
                found[os.path.abspath(entry)] = Path(entry)
    return sorted(found.values(), key=lambda p: os.fspath(p))


def parse_files(
    paths: Iterable[str | Path],
    *,
    max_bytes: int = MAX_XML_BYTES,
) -> ParsedReport:
    """Parse and merge multiple JUnit XML files belonging to one run."""
    report = ParsedReport()
    paths = list(paths)
    if not paths:
        raise ParseError("no JUnit XML files were provided")
    for path in paths:
        for result, warnings in parse_file(path, max_bytes=max_bytes):
            for warning in warnings:
                report.warnings.append(f"{path}: {warning}")
            if result is not None:
                _merge_into(report.results, result)
    return report


def parse_file(
    path: str | Path,
    *,
    max_bytes: int = MAX_XML_BYTES,
) -> Iterator[tuple[TestCaseResult | None, list[str]]]:
    """Yield ``(TestCaseResult | None, warnings)`` pairs for one JUnit XML file.

    ``None`` results carry only warnings (e.g. a ``<testcase>`` without a
    name attribute) so no diagnostic is ever silently dropped.

    Raises :class:`ParseError` for unreadable files, oversized files or
    malformed XML - CI should fail loudly with a clear message rather than
    silently record partial history.
    """
    path = Path(path)
    if not path.is_file():
        raise ParseError(f"JUnit file not found: {path}")
    _check_size(path, max_bytes)
    try:
        if str(path).lower().endswith(".gz"):
            source = io.BytesIO(_read_gzip_capped(path, max_bytes))
        else:
            source = open(path, "rb")  # type: ignore[assignment]
    except OSError as exc:
        raise ParseError(f"{path}: cannot read ({exc})") from exc
    try:
        with source:  # type: ignore[union-attr]
            # Track the open-element stack so processed testcases can be
            # detached from their parent: clearing alone still lets emptied
            # shells accumulate inside a huge <testsuite>.
            context = ET.iterparse(source, events=("start", "end"))
            stack: list[ET.Element] = []
            for event, elem in context:
                if event == "start":
                    stack.append(elem)
                    continue
                stack.pop()
                if not _is_tag(elem, "testcase"):
                    continue
                case_warnings: list[str] = []
                result = _parse_testcase(elem, case_warnings)
                if result is not None:
                    yield result, case_warnings
                elif case_warnings:
                    yield None, case_warnings
                elem.clear()
                if stack:
                    try:
                        stack[-1].remove(elem)
                    except ValueError:
                        pass  # already detached
    except ET.ParseError as exc:
        raise ParseError(f"{path}: malformed XML ({exc})") from exc


def _check_size(path: Path, max_bytes: int) -> int:
    """Pre-check for plain (uncompressed) files; gz is capped while reading."""
    try:
        stat = path.stat()
    except OSError as exc:
        raise ParseError(f"{path}: cannot stat ({exc})") from exc
    if stat.st_size > max_bytes and not str(path).lower().endswith(".gz"):
        raise ParseError(
            f"{path}: file is {stat.st_size} bytes, exceeds the {max_bytes} byte limit"
        )
    return stat.st_size


def _read_gzip_capped(path: Path, max_bytes: int) -> bytes:
    """Decompress up to max_bytes+1, enforcing the cap on the expanded data."""
    try:
        with gzip.open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except gzip.BadGzipFile as exc:
        raise ParseError(f"{path}: not a valid gzip file ({exc})") from exc
    except OSError as exc:
        raise ParseError(f"{path}: cannot read ({exc})") from exc
    if len(data) > max_bytes:
        raise ParseError(
            f"{path}: decompressed content exceeds the {max_bytes} byte limit"
        )
    return data


def _is_tag(elem: ET.Element, name: str) -> bool:
    tag = elem.tag
    if not isinstance(tag, str):
        return False
    if tag.startswith("{"):  # namespaced document
        return tag.rpartition("}")[2] == name
    return tag == name


def _parse_testcase(elem: ET.Element, warnings: list[str]) -> TestCaseResult | None:
    name = elem.get("name")
    if not name or not name.strip():
        warnings.append("skipping <testcase> without a 'name' attribute")
        return None
    classname = elem.get("classname") or None
    file_attr = elem.get("file") or None
    line_attr = elem.get("line")
    line: int | None = None
    if line_attr is not None:
        try:
            line = int(line_attr)
        except ValueError:
            warnings.append(f"non-integer 'line' attribute {line_attr!r} on {name!r}")
    time_attr = elem.get("time")
    duration: float | None = None
    if time_attr is not None:
        try:
            duration = float(time_attr)
        except ValueError:
            pass  # some frameworks emit non-numeric times; not fatal

    status = STATUS_PASS
    flaky_hint = False
    saw_fail_child = False
    retry_attempts: int | None = None
    retry_outcomes = ""
    retry_first_failure = ""
    for child in elem:
        if not isinstance(child.tag, str):
            continue
        tag = child.tag.rpartition("}")[2].lower()
        if tag in {"failure", "error"}:
            saw_fail_child = True
        elif tag in _RERUN_FAILURE_TAGS:
            # pytest-rerunfailures: this element records a *failed attempt*
            # that was rerun. It must count as a failure - parsing it as a
            # pass let a test that failed every retry be recorded as green
            # (fixed in 0.2.0).
            saw_fail_child = True
            flaky_hint = True
        elif tag in _FLAKY_FAILURE_TAGS:
            # Maven Surefire: the test failed on an earlier attempt but
            # ultimately passed - a recovered flake, not a failure.
            flaky_hint = True
        elif tag == "skipped":
            if not saw_fail_child:
                status = STATUS_SKIP
        elif tag == "properties":
            attempts, outcomes, first_failure = _parse_retry_properties(child)
            if attempts is not None:
                retry_attempts = attempts
                retry_outcomes = outcomes
                retry_first_failure = first_failure
        elif "rerun" in tag:
            # Unknown rerun-ish markers (e.g. a bare <rerun>): counted as
            # flakiness evidence only; the status stays whatever the failure
            # children / status attribute say.
            flaky_hint = True
        # system-out / system-err and friends are ignored

    if saw_fail_child:
        status = STATUS_FAIL
    else:
        attr_status = (elem.get("status") or "").strip().lower()
        if attr_status in _FAIL_ATTR_VALUES:
            status = STATUS_FAIL
        elif status != STATUS_SKIP and attr_status in _SKIP_ATTR_VALUES:
            status = STATUS_SKIP

    # A fail-then-pass recovery reported via retry properties is in-run
    # flakiness evidence even when the final testcase element alone looks
    # like a clean pass (the failed attempt is a separate <testcase>).
    if retry_outcomes and _outcomes_recovered(retry_outcomes) and status == STATUS_PASS:
        flaky_hint = True

    return TestCaseResult(
        name=name.strip(),
        classname=classname,
        status=status,
        file=file_attr,
        line=line,
        duration=duration,
        flaky_hint=flaky_hint,
        retry_attempts=retry_attempts,
        retry_outcomes=retry_outcomes,
        retry_first_failure=retry_first_failure,
    )


def _parse_retry_properties(props_elem: ET.Element) -> tuple[int | None, str, str]:
    """Extract flakeherd retry evidence from a <properties> node.

    Returns ``(attempts, outcomes, first_failure)``; attempts is None when
    the node carries no flakeherd retry contract.
    """
    values: dict[str, str] = {}
    for prop in props_elem:
        if not isinstance(prop.tag, str):
            continue
        if prop.tag.rpartition("}")[2].lower() != "property":
            continue
        key = prop.get("name")
        value = prop.get("value")
        if key and value is not None:
            values[key] = value
    if RETRY_PROP_ATTEMPTS not in values:
        return None, "", ""
    try:
        attempts = int(values[RETRY_PROP_ATTEMPTS])
    except ValueError:
        return None, "", ""
    if attempts < 1:
        return None, "", ""
    outcomes = values.get(RETRY_PROP_OUTCOMES, "")
    # Sanity-clamp the outcome string: one letter per attempt, P/F/S only.
    outcomes = ",".join(
        p for p in outcomes.split(",") if p in {"P", "F", "S"}
    )
    first_failure = values.get(RETRY_PROP_FIRST_FAILURE, "")[:MAX_FIRST_FAILURE_EXCERPT]
    return attempts, outcomes, first_failure


def _outcomes_recovered(outcomes: str) -> bool:
    """True when the attempt sequence shows a fail followed by a non-fail."""
    letters = outcomes.split(",")
    seen_fail = False
    for letter in letters:
        if letter == "F":
            seen_fail = True
        elif seen_fail and letter in {"P", "S"}:
            return True
    return False


def _merge_into(results: dict[str, TestCaseResult], result: TestCaseResult) -> None:
    """Merge a parsed case into a per-run result map (see module docstring).

    Duplicate elements for one test key inside a single run come from
    retries/reruns (each attempt emits its own ``<testcase>``) or from
    sharded reports. Rules:

    * pass + fail divergence - the *last* element wins: retry producers
      (pytest-rerunfailures, FlakeHerd's auto-retry) write attempts in
      temporal order, so the final attempt is last. A test that failed its
      final attempt must be recorded as a failure even though an earlier
      attempt (or a bare rerun record) looks like a pass - before 0.2.0 the
      merge always said "recovered", which let a genuinely failing test be
      recorded as green. Divergence itself is kept as ``flaky_hint``.
    * fail + skip - failure wins (a skip cannot un-fail an observed failure).
    * pass + skip - pass wins (0.1.1 behavior).
    """
    key = result.test_key
    existing = results.get(key)
    if existing is None or existing == result:
        if existing is None:
            results[key] = result
        return
    statuses = {existing.status, result.status}
    if STATUS_FAIL in statuses and STATUS_PASS in statuses:
        # Divergent pass/fail duplicates: final attempt (the element parsed
        # last) decides the recorded outcome; the divergence is evidence.
        status = result.status
        flaky_hint = True
    elif STATUS_FAIL in statuses:
        status = STATUS_FAIL
        flaky_hint = existing.flaky_hint or result.flaky_hint
    elif STATUS_PASS in statuses:
        status = STATUS_PASS
        flaky_hint = existing.flaky_hint or result.flaky_hint
    else:
        status = STATUS_SKIP
        flaky_hint = existing.flaky_hint or result.flaky_hint

    # Prefer identity metadata from the failing sample when available: when a
    # failure exists we want annotations to point at it.
    prefer = (
        result
        if result.status == STATUS_FAIL and existing.status != STATUS_FAIL
        else existing
    )
    # Retry evidence: keep the entry with the most attempts (the final
    # attempt's element carries the cumulative picture; duplicate elements
    # without retry metadata - plain shards - do not clobber it).
    retry_source = existing
    if (result.retry_attempts or 0) > (existing.retry_attempts or 0):
        retry_source = result
    results[key] = TestCaseResult(
        name=existing.name,
        classname=existing.classname,
        status=status,
        file=prefer.file or existing.file,
        line=prefer.line if prefer.line is not None else existing.line,
        duration=max(
            (d for d in (existing.duration, result.duration) if d is not None),
            default=None,
        ),
        flaky_hint=flaky_hint,
        retry_attempts=retry_source.retry_attempts,
        retry_outcomes=retry_source.retry_outcomes,
        retry_first_failure=retry_source.retry_first_failure,
    )


def is_ignored(key: str, name: str, classname: str | None, patterns: Iterable[str]) -> bool:
    """Case-insensitive fnmatch against ignore patterns."""
    lowered = [p.lower() for p in patterns]
    return (
        any(fnmatch(key.lower(), p) for p in lowered)
        or any(fnmatch(name.lower(), p) for p in lowered)
        or (classname is not None and any(fnmatch(classname.lower(), p) for p in lowered))
    )
