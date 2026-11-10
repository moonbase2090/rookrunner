# SPDX-License-Identifier: MIT

"""The implementation status lists commands and features that exist in the tree."""

import ast
import importlib
import os
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS = (ROOT / "docs" / "implementation-status.md").read_text()
POINTERS = (
    ROOT / "README.md",
    ROOT / "docs" / "prd.md",
    ROOT / "docs" / "roadmap.md",
)
STALE = (
    "Real workflow execution is not implemented",
    "Real workflows do not execute yet",
    "planner/runtime pending",
    "There is no doctor command, MCP adapter, dashboard, or packaging",
    "MCP is not implemented",
    "`execution_core` 0.0.1",
    "M3 is implemented",
    "are not yet published",
    "The plan merged as a80fce6",
    "| Not started",
)


def fence(label):
    marker = f"```{label}\n"
    start = STATUS.index(marker) + len(marker)
    end = STATUS.index("\n```", start)
    return STATUS[start:end].strip("\n")


def parse_commands(block):
    tops = []
    children = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 1:
            name = parts[0]
            tops.append(name)
            children.setdefault(name, set())
        elif len(parts) == 2:
            parent, child = parts
            tops.append(parent)
            children.setdefault(parent, set()).add(child)
        else:
            raise AssertionError(f"command line has more than two words: {line}")
    return tops, children


def choice_group(help_text):
    lines = help_text.splitlines()
    in_positional = False
    for line in lines:
        if line.startswith("positional arguments:"):
            in_positional = True
            continue
        if in_positional and line and not line.startswith(" "):
            break
        if not in_positional:
            continue
        match = re.search(r"\{([^{}]+)\}", line)
        if match:
            return set(match.group(1).split(","))
    return set()


def cli_help(*args):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    completed = subprocess.run(
        [sys.executable, "-m", "execution_core", *args, "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed


def describe_capabilities(source):
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "capabilities"
                and isinstance(value, ast.List)
            ):
                found.append(
                    tuple(item.value for item in value.elts if isinstance(item, ast.Constant))
                )
    return found


class ImplementationStatusTests(unittest.TestCase):
    def test_package_block_matches_the_tree(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        listed = dict(line.split(": ", 1) for line in fence("status-package").splitlines())
        version = importlib.import_module("execution_core").__version__
        capability = importlib.import_module("execution_core.plan").CAPABILITY_VERSION
        self.assertEqual(listed["version"], version)
        self.assertEqual(listed["version"], project["project"]["version"])
        self.assertEqual(listed["distribution"], project["project"]["name"])
        self.assertEqual(listed["build_backend"], project["build-system"]["build-backend"])
        self.assertEqual(int(listed["capability_version"]), capability)

    def test_listed_commands_are_the_cli(self):
        tops, children = parse_commands(fence("status-commands"))
        listed = set(tops)
        top = cli_help()
        self.assertEqual(top.returncode, 0, top.stderr)
        actual = choice_group(top.stdout)
        self.assertEqual(listed, actual)
        for name in sorted(actual):
            help_text = cli_help(name)
            self.assertEqual(help_text.returncode, 0, help_text.stderr)
            self.assertEqual(children[name], choice_group(help_text.stdout), name)

    def test_absent_commands_are_not_registered(self):
        tops, children = parse_commands(fence("status-commands"))
        present = set(tops)
        for names in children.values():
            present.update(names)
        for name in fence("status-absent").splitlines():
            self.assertNotIn(name.strip(), present)
        top = cli_help()
        actual = choice_group(top.stdout)
        for name in fence("status-absent").splitlines():
            self.assertNotIn(name.strip(), actual)

    def test_listed_methods_are_the_protocol(self):
        listed = [line.strip() for line in fence("status-methods").splitlines() if line.strip()]
        methods = importlib.import_module("execution_core.protocol").METHODS
        self.assertEqual(listed, list(methods))

    def test_listed_capabilities_are_the_describe_list(self):
        listed = [
            line.strip() for line in fence("status-capabilities").splitlines() if line.strip()
        ]
        source = (ROOT / "src" / "execution_core" / "worker.py").read_text()
        found = describe_capabilities(source)
        self.assertEqual(len(found), 1)
        self.assertEqual(tuple(listed), found[0])

    def test_listed_features_name_importable_attributes(self):
        for raw in fence("status-features").splitlines():
            line = raw.strip()
            if not line:
                continue
            label, target = line.split(" | ", 1)
            self.assertTrue(label.strip())
            module_name, attribute = target.split(":", 1)
            module = importlib.import_module(module_name)
            self.assertTrue(hasattr(module, attribute), line)
            self.assertIsNotNone(getattr(module, attribute))

    def test_other_docs_point_here_and_do_not_restate_stale_status(self):
        for path in POINTERS:
            text = path.read_text()
            self.assertIn("implementation-status.md", text, path.name)
            for phrase in STALE:
                self.assertNotIn(phrase, text, f"{path.name} still says {phrase}")


if __name__ == "__main__":
    unittest.main()
