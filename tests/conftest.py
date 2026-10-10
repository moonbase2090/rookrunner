# SPDX-License-Identifier: MPL-2.0

import os
import sys


def pytest_plugin_registered(plugin, manager):
    """Scorecard keeps the first four pytest lines. On CI, leave only the failure."""

    if os.environ.get("CI") and manager.get_name(plugin) == "terminalreporter":
        manager.unregister(plugin)


def pytest_runtest_logreport(report):
    if not os.environ.get("CI") or not report.failed:
        return
    text = getattr(report, "longreprtext", "") or ""
    detail = ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            detail = line
            break
    message = f"FAILED {report.nodeid} {detail}"[:350]
    stream = sys.__stdout__
    if stream is None:
        return
    stream.write(message + "\n")
    stream.flush()
