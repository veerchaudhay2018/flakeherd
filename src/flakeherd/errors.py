"""Error types used across FlakeHerd.

Exit-code convention used by the CLI:
    0 - success (report produced; gate, if enabled, found nothing to fail on)
    1 - gate triggered (``--fail-on`` matched, see CLI docs)
    2 - usage or data error (bad flags, no files matched, unparseable input)
"""

from __future__ import annotations


class FlakeHerdError(Exception):
    """Base class for expected, user-facing errors.

    These carry a human-readable message suitable for printing directly to
    the terminal (already free of stack-trace noise). Unexpected exceptions
    are allowed to propagate so bugs surface loudly.
    """


class ParseError(FlakeHerdError):
    """A JUnit XML file could not be parsed."""


class StoreError(FlakeHerdError):
    """The SQLite history database could not be opened or updated."""


class SyncError(FlakeHerdError):
    """The git-based history sync (cache branch) failed."""


class ManifestError(FlakeHerdError):
    """A quarantine manifest could not be read or is invalid."""
