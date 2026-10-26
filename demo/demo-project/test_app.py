"""A tiny project used by demo/run_demo.sh (Part B).

`test_flaky_network` simulates a flaky test *deterministically*: the
outcome for each (DEMO_SEED, DEMO_RUN) pair is fixed, so the demo is fully
reproducible, while across runs it behaves exactly like real-world
flakiness - same code, different result.
"""

import os
import random


def test_addition():
    assert 1 + 1 == 2


def test_sorting():
    assert sorted([3, 1, 2]) == [1, 2, 3]


def test_flaky_network():
    seed = os.environ.get("DEMO_SEED", "flakeherd-demo")
    run = os.environ.get("DEMO_RUN", "0")
    draw = random.Random(f"{seed}:{run}").randint(0, 9)
    assert draw > 3, f"simulated network timeout (draw={draw})"
