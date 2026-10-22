"""Emit actual legacy pytest outcomes in the official SWE-bench parser format.

Loaded only in the isolated Requests grading container. No test is selected,
skipped or changed; setup/teardown failures remain errors even after a passing
call. Legacy pytest's -rA output otherwise omits the per-test result inventory.
"""


def pytest_runtest_logreport(report):
    if report.failed:
        status = "FAILED" if report.when == "call" else "ERROR"
    elif report.skipped:
        status = "XFAIL" if hasattr(report, "wasxfail") else "SKIPPED"
    elif report.when == "call" and report.passed:
        status = "PASSED"
    else:
        return
    print("\n" + status + " " + report.nodeid, flush=True)
