import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from execution_core.actions import StoredAction
from execution_core.artifacts import named_manifest, written_files
from execution_core.plan import CAPABILITY_VERSION, PlanError, plan_workflow
from execution_core.protocol import canonical
from execution_core.run import (
    RunError,
    _ATTEMPT,
    _consider_step,
    _plan_parts,
    _run_composite,
    _run_upload,
    _write_job_scripts,
)
from execution_core.upload import (
    NO_FILES_LINE,
    SARIF_NAME,
    UploadBook,
    UploadError,
    load_uploads,
    select_files,
    select_sarif,
)
from execution_core.worker import Worker, now
from schema_support import validate_response, validator


UPLOAD = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
SARIF = "github/codeql-action/upload-sarif@2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2"
OTHER = "actions/upload-artifact@" + "a" * 40
IMAGE = "sha256:" + "cd" * 32
EVENT = {"kind": "local", "n": 1}
WORKFLOW = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


def _request(method, params):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    validator("Request").validate(body)
    return canonical(body).encode()


class _Store:
    def __init__(self, root=None):
        self.root = root
        self.calls = []

    def resolve(self, owner, repository, path, ref):
        self.calls.append((owner, repository, path, ref))
        if self.root is None:
            raise AssertionError("fetched")
        return StoredAction(self.root, "ab" * 32)


def _workflow(uses, with_text):
    return (
        f"on: push\njobs:\n  build:\n    steps:\n      - uses: {uses}\n        with:\n{with_text}"
    )


class SelectTests(unittest.TestCase):
    def workspace(self, root):
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "out").mkdir()
        (workspace / "out" / "a.txt").write_bytes(b"a")
        (workspace / "out" / "b.txt").write_bytes(b"b")
        (workspace / "my file.txt").write_bytes(b"space")
        (workspace / ".secret").write_bytes(b"hidden")
        (workspace / "link").symlink_to(workspace / "out" / "a.txt")
        (workspace / "out" / "dir-link").symlink_to(workspace / "out")
        nested = workspace / "out" / "nested"
        nested.mkdir()
        (nested / "c.txt").write_bytes(b"c")
        git = workspace / ".git"
        git.mkdir()
        (git / "config").write_bytes(b"git")
        return workspace

    def test_patterns(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.workspace(Path(directory))
            self.assertEqual(
                select_files(workspace, "out/a.txt", include_hidden=False),
                ["out/a.txt"],
            )
            self.assertEqual(
                select_files(workspace, "./out/a.txt", include_hidden=False),
                ["out/a.txt"],
            )
            self.assertEqual(
                select_files(workspace, "my file.txt", include_hidden=False),
                ["my file.txt"],
            )
            self.assertEqual(
                select_files(workspace, "out", include_hidden=False),
                ["out/a.txt", "out/b.txt", "out/nested/c.txt"],
            )
            self.assertEqual(
                select_files(workspace, "out/", include_hidden=False),
                ["out/a.txt", "out/b.txt", "out/nested/c.txt"],
            )
            self.assertEqual(
                select_files(workspace, "out/*", include_hidden=False),
                ["out/a.txt", "out/b.txt"],
            )
            self.assertEqual(
                select_files(workspace, "out/**/c.txt", include_hidden=False),
                ["out/nested/c.txt"],
            )
            self.assertEqual(
                select_files(workspace, "out/**/a.txt", include_hidden=False),
                ["out/a.txt"],
            )
            self.assertEqual(
                select_files(
                    workspace, "out/a.txt\n\nout/b.txt\n!out/b.txt\n", include_hidden=False
                ),
                ["out/a.txt"],
            )
            self.assertEqual(select_files(workspace, "link", include_hidden=False), [])
            self.assertEqual(select_files(workspace, ".secret", include_hidden=False), [])
            self.assertEqual(
                select_files(workspace, ".secret", include_hidden=True),
                [".secret"],
            )
            self.assertEqual(
                select_files(workspace, ".git/config", include_hidden=True),
                [".git/config"],
            )
            self.assertEqual(select_files(workspace, "out/a.txt/", include_hidden=False), [])
            listed = select_files(workspace, ".", include_hidden=False)
            self.assertIn("out/a.txt", listed)
            self.assertNotIn(".secret", listed)
            self.assertNotIn(".git/config", listed)

    def test_bad_patterns_fail_before_a_match(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.workspace(Path(directory))
            for pattern in (
                "..",
                "../out",
                "/etc/passwd",
                "~",
                "~/file",
                "out/a?.txt",
                "out/[a].txt",
                "out\\a.txt",
                "pre*",
                "a//b",
                "foo/./bar",
                "!",
                "out/a.txt\n..",
            ):
                with self.subTest(pattern=pattern):
                    with self.assertRaises(UploadError) as raised:
                        select_files(workspace, pattern, include_hidden=False)
                    self.assertEqual(str(raised.exception), "path is not accepted")

    def test_sarif_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.workspace(Path(directory))
            (workspace / "results.sarif").write_bytes(b"{}")
            self.assertEqual(select_sarif(workspace, "results.sarif"), "results.sarif")
            self.assertEqual(select_sarif(workspace, "./.secret"), ".secret")
            for text, message in (
                ("missing.sarif", "sarif file is missing"),
                ("out", "path is not accepted"),
                ("../results", "path is not accepted"),
                ("/tmp/results.sarif", "path is not accepted"),
                ("~", "path is not accepted"),
                ("*.sarif", "path is not accepted"),
                ("a.sarif\nb.sarif", "path is not accepted"),
                ("link", "path is not accepted"),
            ):
                with self.subTest(text=text):
                    with self.assertRaises(UploadError) as raised:
                        select_sarif(workspace, text)
                    self.assertEqual(str(raised.exception), message)


class BookTests(unittest.TestCase):
    def test_names_paths_and_the_job_cap(self):
        book = UploadBook()
        book.add("build", "one", ["a.txt"])
        with self.assertRaises(UploadError) as reused:
            book.add("other", "one", ["b.txt"])
        self.assertEqual(str(reused.exception), "artifact name is already used")
        with self.assertRaises(UploadError) as same_path:
            book.add("other", "two", ["a.txt"])
        self.assertEqual(str(same_path.exception), "path is already selected")
        for index in range(1, 500):
            book.add("build", f"name-{index}", [f"file-{index}.txt"])
        with self.assertRaises(UploadError) as limited:
            book.add("build", "over", ["over.txt"])
        self.assertEqual(str(limited.exception), "job artifact limit is 500")
        book.add("test", "other-job", ["other.txt"])
        self.assertEqual(len(book.records), 501)
        with self.assertRaises(UploadError) as bad_name:
            book.add("test", "bad\nname", ["c.txt"])
        self.assertEqual(str(bad_name.exception), "artifact name is not accepted")

    def test_record_file_is_private_and_a_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = root / "uploads.json"
            book = UploadBook(record)
            book.add("build", "coverage", ["out/a.txt"])
            self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
            self.assertEqual(load_uploads(record), book.records)
            self.assertEqual(load_uploads(root / "missing.json"), [])
            record.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_uploads(record), [])
            target = root / "elsewhere.json"
            target.write_text("kept", encoding="utf-8")
            link = root / "linked.json"
            link.symlink_to(target)
            linked = UploadBook(link)
            with self.assertRaises(UploadError):
                linked.add("build", "coverage", ["out/a.txt"])
            self.assertEqual(target.read_text(encoding="utf-8"), "kept")
            self.assertEqual(linked.records, [])


class PlanUploadTests(unittest.TestCase):
    def plan(self, workflow, **kwargs):
        return plan_workflow(workflow.encode(), "build", **kwargs)["plan"]

    def reject(self, workflow, **kwargs):
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), "build", **kwargs)
        return raised.exception

    def test_scorecard_shape_and_multiline_path_plan(self):
        self.assertEqual(CAPABILITY_VERSION, 12)
        workflow = _workflow(
            UPLOAD,
            "          name: coverage\n          path: coverage.txt\n"
            "          if-no-files-found: warn\n",
        )
        plan = self.plan(workflow)
        self.assertEqual(plan["capability_version"], 12)
        step = plan["job"]["steps"][0]
        self.assertEqual(step["uses"], UPLOAD)
        self.assertEqual(step["upload"], "files")
        self.assertEqual(
            step["with"],
            {"name": "coverage", "path": "coverage.txt", "if-no-files-found": "warn"},
        )
        for absent in (
            "run",
            "action_path",
            "action_digest",
            "steps",
            "inputs",
            "outputs",
            "javascript",
            "checkout",
        ):
            self.assertNotIn(absent, step)
        _plan_parts(plan)
        multiline = _workflow(OTHER, "          path: |\n            a.txt\n            b.txt\n")
        recorded = self.plan(multiline)["job"]["steps"][0]
        self.assertEqual(recorded["upload"], "files")
        self.assertIn("\n", recorded["with"]["path"])
        self.assertIn("a.txt", recorded["with"]["path"])
        self.assertIn("b.txt", recorded["with"]["path"])
        check = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "check.yml"
        text = check.read_text(encoding="utf-8")
        self.assertNotIn("upload-artifact", text)
        self.assertNotIn("upload-sarif", text)

    def test_local_composite_upload_plans_and_remote_steps_stay_run_steps(self):
        workflow = "on: push\njobs:\n  build:\n    steps:\n      - uses: ./action\n"
        action = (
            "name: Upload\n"
            "description: Upload files\n"
            "runs:\n"
            "  using: composite\n"
            "  steps:\n"
            f"    - uses: {UPLOAD}\n"
            "      with:\n"
            "        name: coverage\n"
            "        path: out/a.txt\n"
            "        if-no-files-found: warn\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "action").mkdir()
            (root / "action" / "action.yml").write_text(action, encoding="utf-8")
            plan = self.plan(workflow, action_root=root)
        inner = plan["job"]["steps"][0]["steps"][0]
        self.assertEqual(inner["upload"], "files")
        self.assertEqual(inner["with"]["name"], "coverage")
        self.assertIsNone(inner["shell"])
        self.assertNotIn("run", inner)
        _plan_parts(plan)
        remote = root = None
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            (remote / "action.yml").write_text(action, encoding="utf-8")
            store = _Store(remote)
            uses = "acme/demo@" + "d" * 40
            error = self.reject(
                f"on: push\njobs:\n  build:\n    steps:\n      - uses: {uses}\n",
                action_store=store,
            )
            self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
            self.assertEqual(len(store.calls), 1)

    def test_rejected_inputs_and_uses_name_the_field(self):
        cases = (
            ("retention-days: 1\n", "CAPABILITY_UNSUPPORTED", "retention-days"),
            ("compression-level: 6\n", "CAPABILITY_UNSUPPORTED", "compression-level"),
            ("overwrite: true\n", "CAPABILITY_UNSUPPORTED", "overwrite"),
            ("include-hidden-files: yes\n", "WORKFLOW_INVALID", "include-hidden-files"),
            ("archive: 'false'\n", "WORKFLOW_INVALID", "archive"),
            ("if-no-files-found: nope\n", "WORKFLOW_INVALID", "if-no-files-found"),
        )
        for extra, kind, key in cases:
            workflow = _workflow(UPLOAD, f"          path: out/a.txt\n          {extra}")
            with self.subTest(key=key):
                error = self.reject(workflow)
                self.assertEqual(error.kind, kind)
                self.assertEqual(error.field, f"jobs.build.steps.0.with.{key}")
        hashed = self.reject(_workflow(UPLOAD, "          path: ${{ hashFiles('*.txt') }}\n"))
        self.assertEqual(hashed.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(hashed.field, "jobs.build.steps.0.with.path")
        missing = self.reject(_workflow(UPLOAD, "          name: coverage\n"))
        self.assertEqual(missing.kind, "WORKFLOW_INVALID")
        self.assertEqual(missing.field, "jobs.build.steps.0.with.path")
        sarif = self.reject(
            _workflow(
                SARIF, "          token: ${{ github.token }}\n          sarif_file: r.sarif\n"
            )
        )
        self.assertEqual(sarif.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(sarif.field, "jobs.build.steps.0.with.token")
        omitted = self.reject(f"on: push\njobs:\n  build:\n    steps:\n      - uses: {SARIF}\n")
        self.assertEqual(omitted.kind, "WORKFLOW_INVALID")
        self.assertEqual(omitted.field, "jobs.build.steps.0.with.sarif_file")
        store = _Store()
        for uses in (
            "actions/download-artifact@" + "e" * 40,
            "github/codeql-action/init@" + "e" * 40,
            "github/codeql-action/analyze@" + "e" * 40,
            "actions/upload-artifact@v7",
            "actions/upload-artifact",
            "actions/upload-artifact@" + "a" * 12,
        ):
            with self.subTest(uses=uses):
                error = self.reject(
                    f"on: push\njobs:\n  build:\n    steps:\n      - uses: {uses}\n",
                    action_store=store,
                )
                self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(error.field, "jobs.build.steps.0.uses")
        self.assertEqual(store.calls, [])

    def test_write_permissions_still_fail_planning(self):
        for scope in ("security-events", "actions"):
            workflow = (
                "permissions:\n"
                f"  {scope}: write\n"
                "on: push\n"
                "jobs:\n"
                "  build:\n"
                "    steps:\n"
                f"      - uses: {UPLOAD}\n"
                "        with:\n"
                "          path: coverage.txt\n"
            )
            with self.subTest(scope=scope):
                error = self.reject(workflow)
                self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(error.field, f"permissions.{scope}")

    def test_version_11_plan_is_not_migrated(self):
        plan = self.plan(_workflow(UPLOAD, "          path: coverage.txt\n"))
        plan["capability_version"] = 11
        with self.assertRaises(RunError) as raised:
            _plan_parts(plan)
        self.assertEqual(str(raised.exception), "plan is not accepted")


class RunUploadTests(unittest.TestCase):
    def run_step(self, workspace, step, book):
        token = _ATTEMPT.set({"uploads": book, "arch": "X64"})
        try:
            with (
                patch("socket.socket", side_effect=AssertionError("network")),
                patch("execution_core.run.subprocess.run", side_effect=AssertionError("process")),
                patch("execution_core.run.subprocess.Popen", side_effect=AssertionError("process")),
            ):
                return _run_upload(
                    step,
                    workspace,
                    {"github": {}, "env": {}, "vars": {}, "inputs": {}},
                    "build",
                )
        finally:
            _ATTEMPT.reset(token)

    def files_step(self, **with_values):
        body = {"path": "out/a.txt"}
        body.update(with_values)
        return {
            "index": 0,
            "id": "up",
            "name": "upload",
            "uses": UPLOAD,
            "upload": "files",
            "env": {},
            "with": body,
        }

    def test_records_files_without_a_zip_or_a_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            (workspace / "out").mkdir(parents=True)
            (workspace / "out" / "a.txt").write_bytes(b"a")
            (workspace / "out" / "b.txt").write_bytes(b"b")
            book = UploadBook(root / "uploads.json")
            result = self.run_step(
                workspace,
                self.files_step(name="coverage", path="out/a.txt\nout/b.txt"),
                book,
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["stdout"], "")
            self.assertEqual(
                book.records, [{"name": "coverage", "paths": ["out/a.txt", "out/b.txt"]}]
            )
            self.assertEqual(list(workspace.rglob("*.zip")), [])
            one = UploadBook()
            named = self.run_step(
                workspace,
                self.files_step(**{"archive": False, "name": "", "path": "out/a.txt"}),
                one,
            )
            self.assertEqual(named["status"], "succeeded")
            self.assertEqual(one.records, [{"name": "a.txt", "paths": ["out/a.txt"]}])
            many = UploadBook()
            failed = self.run_step(
                workspace,
                self.files_step(**{"archive": False, "path": "out/*"}),
                many,
            )
            self.assertEqual(failed["exit_code"], 1)
            self.assertEqual(failed["stderr"], "archive false accepts one file\n")
            self.assertEqual(many.records, [])

    def test_empty_match_names_and_paths(self):
        empty_line = "No files were found for the artifact.\n"
        self.assertEqual(NO_FILES_LINE, empty_line)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            warned = UploadBook()
            warn = self.run_step(workspace, self.files_step(path="missing.txt"), warned)
            self.assertEqual(warn["status"], "succeeded")
            self.assertEqual(warn["exit_code"], 0)
            self.assertEqual(warn["stdout"], empty_line)
            self.assertEqual(warned.records, [])
            ignored = UploadBook()
            ignore = self.run_step(
                workspace,
                self.files_step(**{"path": "missing.txt", "if-no-files-found": "ignore"}),
                ignored,
            )
            self.assertEqual(ignore["stdout"], "")
            self.assertEqual(ignore["exit_code"], 0)
            self.assertEqual(ignored.records, [])
            errored = UploadBook()
            error = self.run_step(
                workspace,
                self.files_step(**{"path": "missing.txt", "if-no-files-found": "error"}),
                errored,
            )
            self.assertEqual(error["status"], "failed")
            self.assertEqual(error["exit_code"], 1)
            self.assertEqual(error["stderr"], "no files were found\n")
            self.assertIsNone(error["error"])
            parent = self.run_step(workspace, self.files_step(path="../secret"), UploadBook())
            self.assertEqual(parent["stderr"], "path is not accepted\n")
            (workspace / "out").mkdir()
            (workspace / "out" / "a.txt").write_bytes(b"a")
            book = UploadBook()
            self.run_step(workspace, self.files_step(name="coverage"), book)
            again = self.run_step(workspace, self.files_step(name="coverage"), book)
            self.assertEqual(again["stderr"], "artifact name is already used\n")
            rooted_step = self.files_step(path="a.txt", **{"if-no-files-found": "error"})
            rooted_step["working_directory"] = "out"
            rooted = self.run_step(workspace, rooted_step, UploadBook())
            self.assertEqual(rooted["stderr"], "no files were found\n")
            skipped = _consider_step(
                "docker",
                "name",
                {**self.files_step(), "if": "false"},
                {"env": {}},
                {"id": "build", "env": {}},
                {},
                workspace,
                True,
                None,
                None,
                [],
                False,
                {},
                None,
                None,
                None,
            )
            self.assertEqual(skipped["status"], "skipped")
            self.assertEqual(book.records, [{"name": "coverage", "paths": ["out/a.txt"]}])

    def test_sarif_and_expression_path(self):
        sarif_name = "codeql-sarif"
        self.assertEqual(SARIF_NAME, sarif_name)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            (workspace / "results.sarif").write_bytes(b"{}")
            (workspace / "out").mkdir()
            (workspace / "out" / "a.txt").write_bytes(b"a")
            book = UploadBook()
            sarif = self.run_step(
                workspace,
                {
                    "index": 1,
                    "uses": SARIF,
                    "upload": "sarif",
                    "env": {},
                    "with": {"sarif_file": "results.sarif"},
                },
                book,
            )
            self.assertEqual(sarif["exit_code"], 0)
            expressed = self.run_step(
                workspace,
                self.files_step(path="${{ 'out/a.txt' }}", name="coverage"),
                book,
            )
            self.assertEqual(expressed["exit_code"], 0)
            self.assertEqual(
                book.records,
                [
                    {"name": sarif_name, "paths": ["results.sarif"]},
                    {"name": "coverage", "paths": ["out/a.txt"]},
                ],
            )
            clash = self.run_step(
                workspace, self.files_step(name=sarif_name, path="out/a.txt"), book
            )
            self.assertEqual(clash["stderr"], "artifact name is already used\n")

    def test_local_composite_upload_writes_no_script(self):
        workflow = "on: push\njobs:\n  build:\n    steps:\n      - uses: ./action\n"
        action = (
            "name: Upload\ndescription: Upload files\nruns:\n  using: composite\n  steps:\n"
            f"    - uses: {UPLOAD}\n      with:\n        path: out/a.txt\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "action").mkdir()
            (root / "action" / "action.yml").write_text(action, encoding="utf-8")
            plan = plan_workflow(workflow.encode(), "build", action_root=root)["plan"]
            private = root / "scripts"
            private.mkdir()
            scripts = {}
            _write_job_scripts(plan["jobs"], (), private, scripts)
            self.assertEqual(scripts[(("build",), 0)], [None])
            self.assertEqual(list(private.iterdir()), [])
            workspace = root / "workspace"
            (workspace / "out").mkdir(parents=True)
            (workspace / "out" / "a.txt").write_bytes(b"a")
            book = UploadBook(root / "uploads.json")
            token = _ATTEMPT.set({"uploads": book, "arch": "X64"})
            try:
                result = _run_composite(
                    "docker",
                    "name",
                    plan["job"]["steps"][0],
                    {"env": {}},
                    {"id": "build", "env": {}},
                    {},
                    workspace,
                    True,
                    None,
                    time.monotonic() + 30,
                    [],
                    False,
                    {},
                    [None],
                    None,
                    None,
                    "build",
                )
            finally:
                _ATTEMPT.reset(token)
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(book.records, [{"name": "artifact", "paths": ["out/a.txt"]}])


class ManifestTests(unittest.TestCase):
    def test_selected_files_keep_one_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            files = snapshot / "files"
            files.mkdir(parents=True)
            (files / "keep.txt").write_bytes(b"same")
            (files / "source.txt").write_bytes(b"original")
            workspace = root / "workspace"
            shutil.copytree(files, workspace)
            (workspace / "source.txt").write_bytes(b"changed")
            (workspace / "out").mkdir()
            (workspace / "out" / "a.txt").write_bytes(b"new")
            git = workspace / ".git"
            git.mkdir()
            (git / "config").write_bytes(b"git")
            selections = [
                {
                    "name": "coverage",
                    "paths": ["keep.txt", "out/a.txt", "out/gone.txt", ".git/config"],
                }
            ]
            entries, missing = named_manifest(workspace, snapshot, selections)
            by_path = {item["path"]: item for item in entries}
            self.assertEqual(missing, ["out/gone.txt"])
            self.assertEqual(by_path["keep.txt"]["name"], "coverage")
            self.assertEqual(by_path["out/a.txt"]["name"], "coverage")
            self.assertEqual(by_path[".git/config"]["name"], "coverage")
            self.assertNotIn("name", by_path["source.txt"])
            self.assertEqual(len(entries), len(by_path))
            plain, nothing = named_manifest(workspace, snapshot, None)
            self.assertEqual(plain, written_files(workspace, snapshot))
            self.assertEqual(nothing, [])
            self.assertNotIn(".git/config", {item["path"] for item in plain})


class PublishTests(unittest.TestCase):
    def git_workflow(self, repo):
        workflow = repo / ".github" / "workflows" / "test.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(WORKFLOW, encoding="utf-8")
        (repo / "source.txt").write_text("original\n", encoding="utf-8")
        (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", repo, "init", "--initial-branch=main"], check=True, capture_output=True
        )
        subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                repo,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            check=True,
            capture_output=True,
        )

    def prepare(self, directory):
        root = Path(directory)
        repo = root / "repo"
        state = root / "state"
        self.git_workflow(repo)
        worker = Worker(repo, state)
        worker.execute_queue = lambda: worker.stop.wait()
        worker.start()
        accepted = worker.response(
            _request(
                "run.submit",
                {
                    "version": 1,
                    "submission_key": "wf",
                    "workflow": ".github/workflows/test.yml",
                    "job_id": "build",
                    "event": EVENT,
                    "image": IMAGE,
                },
            )
        )
        validate_response("run.submit", accepted)
        record = accepted["result"]
        attempt = state / "attempts" / record["run_id"]
        attempt.mkdir(parents=True)
        workspace = attempt / "workspace"
        snapshot = state / "snapshots" / record["input"]["snapshot_id"]
        shutil.copytree(snapshot / "files", workspace, symlinks=True)
        (workspace / "source.txt").write_bytes(b"changed")
        (workspace / "out").mkdir()
        (workspace / "out" / "a.txt").write_bytes(b"named")
        record.update(state="running", started_at=now(), attempt_id=record["run_id"])
        with worker.db:
            worker.save(record)
        return worker, record, workspace

    def names(self, worker, run_id):
        reply = worker.response(_request("run.artifacts", {"run_id": run_id}))
        validate_response("run.artifacts", reply)
        return reply["result"]["artifacts"]

    def test_publish_names_selected_files_and_fails_a_missing_one(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, record, _workspace = self.prepare(directory)
            try:
                uploads = [
                    {
                        "name": "coverage",
                        "paths": ["keep.txt", "out/a.txt", "out/gone.txt"],
                    }
                ]
                with worker.db:
                    worker._save_workflow_outcome(
                        record["run_id"],
                        record,
                        {
                            "status": "succeeded",
                            "exit_code": 0,
                            "image_digest": record["input"]["image_digest"],
                            "steps": [],
                            "uploads": uploads,
                        },
                    )
                self.assertEqual(record["state"], "failed")
                self.assertEqual(record["error"]["kind"], "STEP_FAILED")
                self.assertEqual(
                    record["error"]["message"],
                    "selected artifact file is missing: out/gone.txt",
                )
                listed = {item["path"]: item for item in self.names(worker, record["run_id"])}
                self.assertEqual(listed["keep.txt"]["name"], "coverage")
                self.assertEqual(listed["out/a.txt"]["name"], "coverage")
                self.assertNotIn("name", listed["source.txt"])
                self.assertNotIn("out/gone.txt", listed)
                self.assertEqual(
                    listed["out/a.txt"]["digest"], hashlib.sha256(b"named").hexdigest()
                )
            finally:
                worker.close()

    def test_cancel_and_an_earlier_failure_stay(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, record, _workspace = self.prepare(directory)
            try:
                outcome = {
                    "status": "cancelled",
                    "exit_code": None,
                    "image_digest": record["input"]["image_digest"],
                    "steps": [],
                    "uploads": [{"name": "coverage", "paths": ["out/gone.txt", "out/a.txt"]}],
                }
                with worker.db:
                    worker._save_workflow_outcome(record["run_id"], record, outcome)
                self.assertEqual(record["state"], "cancelled")
                self.assertIsNone(record["error"])
                listed = {item["path"]: item for item in self.names(worker, record["run_id"])}
                self.assertEqual(listed["out/a.txt"]["name"], "coverage")
                self.assertNotIn("out/gone.txt", listed)
            finally:
                worker.close()
        with tempfile.TemporaryDirectory() as directory:
            worker, record, _workspace = self.prepare(directory)
            try:
                outcome = {
                    "status": "failed",
                    "exit_code": 7,
                    "image_digest": record["input"]["image_digest"],
                    "steps": [],
                    "uploads": [{"name": "coverage", "paths": ["out/gone.txt"]}],
                }
                with worker.db:
                    worker._save_workflow_outcome(record["run_id"], record, outcome)
                self.assertEqual(record["state"], "failed")
                self.assertEqual(record["exit_code"], 7)
                self.assertIsNone(record["error"])
            finally:
                worker.close()

    def test_attempt_file_names_a_restart_and_a_lost_run(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, record, workspace = self.prepare(directory)
            try:
                payload = {"uploads": [{"name": "coverage", "paths": ["keep.txt", "out/gone.txt"]}]}
                (workspace.parent / "uploads.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                os.chmod(workspace.parent / "uploads.json", 0o600)
                with worker.db:
                    worker._publish_artifacts(record)
                listed = {item["path"]: item for item in self.names(worker, record["run_id"])}
                self.assertEqual(listed["keep.txt"]["name"], "coverage")
                self.assertNotIn("name", listed["source.txt"])
                worker._commit_lost(record, None)
                self.assertEqual(worker.get(record["run_id"])["state"], "lost")
            finally:
                worker.close()

    def test_a_plan_without_uploads_keeps_the_ns12_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, record, _workspace = self.prepare(directory)
            try:
                with worker.db:
                    worker._save_workflow_outcome(
                        record["run_id"],
                        record,
                        {
                            "status": "succeeded",
                            "exit_code": 0,
                            "image_digest": record["input"]["image_digest"],
                            "steps": [],
                        },
                    )
                self.assertEqual(record["state"], "succeeded")
                for item in self.names(worker, record["run_id"]):
                    self.assertNotIn("name", item)
            finally:
                worker.close()

    def test_existing_artifact_table_gains_a_name_column(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            state = root / "state"
            state.mkdir()
            os.chmod(state, 0o700)
            connection = sqlite3.connect(state / "runs.sqlite3")
            connection.execute(
                """
                CREATE TABLE artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    UNIQUE(run_id, path)
                )
                """
            )
            connection.commit()
            connection.close()
            worker = Worker(root / "repo", state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                columns = [row[1] for row in worker.db.execute("PRAGMA table_info(artifacts)")]
                self.assertIn("name", columns)
            finally:
                worker.close()
