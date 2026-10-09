import sys


def pytest_plugin_registered(plugin, manager):
    """Scorecard keeps the first four pytest lines. Leave only the failure."""

    if manager.get_name(plugin) == "terminalreporter":
        manager.unregister(plugin)


def pytest_runtest_logreport(report):
    if not report.failed:
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
