"""FlakeHerd: history-aware flaky-test detection and quarantine for GitHub Actions.

FlakeHerd records per-test outcomes from JUnit XML reports across CI runs,
classifies each test (flaky, regression, broken, fixed, ...) using evidence
from that history, and generates GitHub annotations, job summaries and
quarantine manifests. All history is kept in a local SQLite database that can
be synced through a cache branch inside your own repository - no server, no
account, no telemetry.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
