"""Small shared helpers: time, ids, redaction, formatting, atomic writes."""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from datetime import datetime, timezone

_GITHUB_TOKEN_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b")
_FINE_GRAINED_TOKEN_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")
_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._\-]+")


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with microseconds (sortable, explicit TZ)."""
    return datetime.now(timezone.utc).isoformat()


def new_run_uid(prefix: str = "local") -> str:
    """Generate a unique run id for local invocations.

    In CI the CLI prefers ``$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT`` so that a
    re-run of the same job records *distinct* outcomes for the same commit -
    that pair (same sha, different outcome) is the strongest flakiness
    signal FlakeHerd has.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def redact(text: str) -> str:
    """Mask credential-looking substrings before printing anything."""
    text = _GITHUB_TOKEN_RE.sub("[REDACTED]", text)
    text = _FINE_GRAINED_TOKEN_RE.sub("[REDACTED]", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    return text


def atomic_write_bytes(path: str | os.PathLike, data: bytes) -> None:
    """Write bytes to *path* atomically (temp file + rename, same directory).

    Creates missing parent directories; the temporary file is cleaned up
    on failure.
    """
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".flakeherd-tmp-")
    try:
        with open(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fmt_pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def truncate(text: str, width: int) -> str:
    """Truncate to *width* characters with a plain-ASCII ellipsis."""
    if len(text) <= width:
        return text
    return text[: max(1, width - 3)] + "..."


def clamp(value: int | float, lo: int | float, hi: int | float) -> int | float:
    return max(lo, min(hi, value))


def is_truthy(value: str | None) -> bool:
    """Parse booleans coming from CLI flags, action inputs or env vars."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}
