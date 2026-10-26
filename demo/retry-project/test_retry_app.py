"""A tiny project used by demo/run_demo.sh (Part D: auto-retry).

`test_transient_network` simulates a *transient* failure: it fails the
first time it executes in a process and passes on any retry - the classic
"re-run and it goes green" case. Run with ``--flakeherd-retries 1`` the
failure is retried, recovered, and - unlike a bare rerun - the original
failure plus every attempt's outcome is preserved in the JUnit XML.

`test_permanently_broken` never passes: with retries on it fails *every*
attempt, proving that a genuine failure stays red under auto-retry.

`test_always_green` shows that passing tests carry no retry noise.
"""

_execution_counts = {}


def test_always_green():
    assert True


def test_transient_network():
    count = _execution_counts.get("transient", 0) + 1
    _execution_counts["transient"] = count
    assert count >= 2, f"simulated transient timeout on first call (call {count})"


def test_permanently_broken():
    assert False, "simulated genuine bug (fails every attempt)"
