"""Build the deterministic 8-run CI history used by demo Part A.

Each "run" is one JUnit XML file covering the same nine tests, crafted so
that after eight runs every FlakeHerd classification appears:

    test_valid_login            P*8                    -> stable-pass
    test_retry_on_timeout       P P F P P P P P  (runs 3+4 share a commit:
                                                   a failed run re-ran green)
                                                       -> confirmed-flaky
    test_autocomplete           P F P F P F P P        -> flaky (T=6)
    test_experiment_ratios      P P F F P P P P        -> flaky-low (T=2)
    test_rate_limit_headers     P P P P F F F F        -> regression
    test_eviction_order         F F F F P P P P        -> fixed
    test_multipart_resume       F*8                    -> broken
    test_probe                  skipped*8              -> skip-only
    test_new_widget             appears in run 8 only  -> new

Usage: python build_history.py <output-dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

P, F, S = "pass", "fail", "skip"

CASES = [
    # (classname, name, file, line, [status per run 1..8]; None = not present)
    ("login.UserTest", "test_valid_login", "tests/test_login.py", 12, [P] * 8),
    ("payment.RetryTest", "test_retry_on_timeout", "tests/test_payment.py", 42,
     [P, P, F, P, P, P, P, P]),
    ("search.SearchTest", "test_autocomplete", "tests/test_search.py", 88,
     [P, F, P, F, P, F, P, P]),
    ("flags.FlagTest", "test_experiment_ratios", "tests/test_flags.py", 7,
     [P, P, F, F, P, P, P, P]),
    ("api.RateLimitTest", "test_rate_limit_headers", "tests/test_api.py", 103,
     [P, P, P, P, F, F, F, F]),
    ("cache.EvictionTest", "test_eviction_order", "tests/test_cache.py", 25,
     [F, F, F, F, P, P, P, P]),
    ("upload.UploadTest", "test_multipart_resume", "tests/test_upload.py", 58,
     [F] * 8),
    ("experimental.ProbeTest", "test_probe", "tests/test_experimental.py", 5,
     [S] * 8),
    ("widgets.WidgetTest", "test_new_widget", "tests/test_widgets.py", 14,
     [None, None, None, None, None, None, None, P]),
]

# Runs 3 and 4 model a GitHub "Re-run failed jobs": same commit, rerun.
SHAS = ["commit-0001", "commit-0002", "commit-0003", "commit-0003",
        "commit-0005", "commit-0006", "commit-0007", "commit-0008"]


def build(out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    shas_path = out_dir / "shas.txt"
    shas_path.write_text("\n".join(SHAS) + "\n", encoding="utf-8")
    for run_no in range(1, 9):
        rows = []
        for classname, name, file, line, statuses in CASES:
            status = statuses[run_no - 1]
            if status is None:
                continue
            attrs = f'name="{name}" classname="{classname}" file="{file}" line="{line}"'
            if status == P:
                rows.append(f"<testcase {attrs} time=\"0.05\"/>")
            elif status == F:
                rows.append(
                    f"<testcase {attrs} time=\"0.9\">"
                    '<failure message="assertion failed" type="AssertionError">tb</failure>'
                    "</testcase>"
                )
            else:
                rows.append(f"<testcase {attrs}><skipped/></testcase>")
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<testsuites name="ci-run-{run_no}" tests="{len(rows)}">'
            f'<testsuite name="suite-{run_no}" tests="{len(rows)}">'
            + "".join(rows) +
            "</testsuite></testsuites>"
        )
        (out_dir / f"run_{run_no}.xml").write_text(xml, encoding="utf-8")
    return [f"run_{i}.xml" for i in range(1, 9)]


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ci-history")
    files = build(target)
    print(f"wrote {len(files)} simulated CI reports to {target}/")
    print("run 3 and run 4 share commit-0003 (a failed run re-ran green).")
