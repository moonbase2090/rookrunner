import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from execution_core.actions import ActionStorageFull, stage_node24_actions
from execution_core.disk import usage
from execution_core.plan import plan_snapshot
from execution_core.protocol import canonical
from execution_core.worker import Worker
from schema_support import validate_response, validator


IMAGE = "sha256:" + "cd" * 32
EVENT = {"kind": "local", "n": 1}
SYNTAX = "https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax"
HELLO = """\
name: Hello
description: remote hello
runs:
  using: composite
  steps:
    - shell: bash
      run: printf '%s\\n' remote-ok > "$GITHUB_WORKSPACE/marker.txt"
"""
NESTED = """\
name: Nested
description: nested hello
runs:
  using: composite
  steps:
    - shell: bash
      run: printf '%s\\n' nested-ok
"""
NODE24 = """\
name: Node
description: javascript
outputs:
  answer:
    description: Answer
runs:
  using: node24
  main: index.js
"""
NODE20 = """\
name: Node20
description: old runtime
runs:
  using: node20
  main: index.js
"""
PRE = """\
name: Pre
description: setup entry
runs:
  using: node24
  pre: setup.js
  main: index.js
"""
POST = """\
name: Post
description: cleanup entry
runs:
  using: node24
  main: index.js
  post: cleanup.js
"""
DOCKER = """\
name: Image
description: docker
runs:
  using: docker
  image: Dockerfile
"""


def _git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", repo, *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return result


class RemoteActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="remote-action-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.remote = self.root / "remote"
        self.repo.mkdir()
        self.remote.mkdir()
        _git(self.repo, "init", "--initial-branch=main")
        self.worker = None

    def tearDown(self):
        if self.worker is not None:
            self.worker.close()
        self.tmp.cleanup()

    def _commit(self, repo, message):
        _git(repo, "add", ".")
        _git(
            repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            message,
        )
        return _git(repo, "rev-parse", "HEAD").stdout.strip()

    def _action(self, files, owner="acme", repository="hello"):
        repo = self.remote / owner / f"{repository}.git"
        repo.mkdir(parents=True)
        _git(repo, "init", "--initial-branch=main")
        _git(repo, "config", "uploadpack.allowReachableSHA1InWant", "true")
        _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
        for rel, text in files.items():
            path = repo.joinpath(*PurePosix(rel))
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, tuple):
                os.symlink(text[1], path)
            else:
                path.write_text(text)
        return self._commit(repo, repository)

    def _workflow(self, text, name="test.yml"):
        path = self.repo / ".github" / "workflows" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self._commit(self.repo, name)
        return f".github/workflows/{name}"

    def _start(self, budget=None):
        self.worker = Worker(self.repo, self.state, budget, action_remote=str(self.remote))
        self.worker.execute_queue = lambda: self.worker.stop.wait()
        self.worker.start()
        return self.worker

    def _submit(self, key, workflow):
        params = {
            "version": 1,
            "submission_key": key,
            "workflow": workflow,
            "job_id": "build",
            "event": EVENT,
            "image": IMAGE,
        }
        body = {"jsonrpc": "2.0", "id": 1, "method": "run.submit", "params": params}
        validator("Request").validate(body)
        reply = self.worker.response(canonical(body).encode())
        validate_response("run.submit", reply)
        return reply

    def _uses(self, pin, job="build"):
        return (
            "name: demo\n"
            "on: push\n"
            "jobs:\n"
            f"  {job}:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            f"      - uses: {pin}\n"
        )

    def _row(self, key):
        return self.worker.db.execute(
            "SELECT 1 FROM runs WHERE submission_key=?", (key,)
        ).fetchone()

    def test_action_remote_must_be_github_or_an_absolute_directory(self):
        Worker(self.repo, self.state)
        with self.assertRaises(ValueError):
            Worker(self.repo, self.state, action_remote="remote")
        with self.assertRaises(ValueError):
            Worker(self.repo, self.state, action_remote="https://example.invalid")

    def test_pinned_composite_is_recorded_and_reused(self):
        sha = self._action({"action.yml": HELLO})
        workflow = self._workflow(self._uses(f"acme/hello@{sha}"))
        secret = "SECRET-TOKEN-VALUE"
        config = self.root / "secret.gitconfig"
        config.write_text(f"[http]\n\textraheader = AUTHORIZATION: bearer {secret}\n")
        worker = self._start()
        calls = []
        real_run = subprocess.run

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real_run(*args, **kwargs)

        previous = {
            name: os.environ.get(name) for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT")
        }
        os.environ["GIT_CONFIG_GLOBAL"] = str(config)
        os.environ["GIT_CONFIG_COUNT"] = "1"
        try:
            with patch("execution_core.actions.subprocess.run", spy):
                accepted = self._submit("first", workflow)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertNotIn("error", accepted, accepted)
        result = accepted["result"]
        self.assertEqual(result["state"], "queued")
        recorded = result["input"]["actions"]
        self.assertEqual(recorded[0]["owner"], "acme")
        self.assertEqual(recorded[0]["repository"], "hello")
        self.assertEqual(recorded[0]["path"], "")
        self.assertEqual(recorded[0]["commit"], sha)
        self.assertEqual(len(recorded[0]["digest"]), 64)
        self.assertNotIn(secret, json.dumps(accepted))
        self.assertNotIn(str(self.state), json.dumps(accepted))
        fetches = [args[0] for args, _kwargs in calls if "fetch" in args[0]]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(worker.action_store().fetches, 1)
        fetch = fetches[0]
        self.assertIn("credential.helper=", fetch)
        self.assertIn("http.extraheader=", fetch)
        self.assertIn(f"{self.remote}/acme/hello.git", fetch)
        self.assertNotIn("https://github.com", " ".join(fetch))
        for _args, kwargs in calls:
            env = kwargs.get("env") or {}
            self.assertEqual(env.get("GIT_CONFIG_GLOBAL"), os.devnull)
            self.assertEqual(env.get("GIT_CONFIG_NOSYSTEM"), "1")
            self.assertNotIn("GIT_CONFIG_COUNT", env)
            self.assertNotIn(secret, json.dumps(sorted(env.values())))
            self.assertNotIn(secret, " ".join(_args[0]))
        snapshot = self.state / "snapshots" / result["input"]["snapshot_id"]
        self.assertFalse((snapshot / "files" / "action.yml").exists())
        planned = plan_snapshot(snapshot, "build", action_store=worker.action_store())
        step = planned["plan"]["job"]["steps"][0]
        self.assertEqual(step["action_commit"], sha)
        self.assertEqual(step["content_digest"], recorded[0]["digest"])
        self.assertEqual(step["action_owner"], "acme")
        self.assertEqual(step["action_repository"], "hello")
        self.assertEqual(step["action_path"], "")
        self.assertIn("remote-ok", step["steps"][0]["run"])
        self.assertEqual(planned["digest"], result["input"]["plan_digest"])
        self.assertEqual(worker.action_store().fetches, 1)
        stored = self.state / "actions" / "objects" / recorded[0]["digest"] / "action.yml"
        self.assertIn("remote-ok", stored.read_text())
        original = stored.read_bytes()
        before = usage(self.state)
        stored.write_bytes(original + b"x")
        self.assertGreater(usage(self.state), before)
        stored.write_bytes(original)
        self.assertEqual(usage(self.state), before)
        again = self._submit("second", workflow)
        self.assertNotIn("error", again, again)
        self.assertEqual(again["result"]["input"]["actions"], recorded)
        self.assertEqual(again["result"]["input"]["plan_digest"], result["input"]["plan_digest"])
        self.assertNotEqual(again["result"]["run_id"], result["run_id"])
        self.assertEqual(worker.action_store().fetches, 1)
        retry = self._submit("first", workflow)
        self.assertEqual(retry["result"]["run_id"], result["run_id"])
        self.assertEqual(worker.action_store().fetches, 1)

    def test_tag_short_sha_and_unreachable_repository_consume_no_key(self):
        sha = self._action({"action.yml": HELLO})
        short = sha[:7]
        upper = sha.upper()
        long_sha = "ab" * 32
        cases = {
            "tag": "acme/hello@v1",
            "branch": "acme/hello@main",
            "short": f"acme/hello@{short}",
            "upper": f"acme/hello@{upper}",
            "sha256": f"acme/hello@{long_sha}",
        }
        self._start()
        for name, pin in cases.items():
            workflow = self._workflow(self._uses(pin), f"{name}.yml")
            with self.subTest(name=name):
                reply = self._submit(name, workflow)
                self.assertEqual(reply["error"]["data"]["kind"], "CAPABILITY_UNSUPPORTED")
                self.assertIn("40-character lowercase commit SHA", reply["error"]["message"])
                self.assertIn(SYNTAX, reply["error"]["message"])
                self.assertIsNone(self._row(name))
                self.assertNotIn(str(self.state), json.dumps(reply))
        self.assertEqual(self.worker.action_store().fetches, 0)
        missing = self._workflow(self._uses(f"missing/repo@{sha}"), "missing.yml")
        refused = self._submit("missing", missing)
        self.assertEqual(refused["error"]["data"]["kind"], "ACTION_UNAVAILABLE")
        self.assertIn("could not be fetched", refused["error"]["message"])
        self.assertNotIn(str(self.remote), refused["error"]["message"])
        self.assertNotIn(str(self.state), json.dumps(refused))
        self.assertIsNone(self._row("missing"))
        self.assertEqual(self.worker.action_store().fetches, 1)
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        snaps = self.state / "snapshots"
        self.assertEqual([] if not snaps.exists() else list(snaps.iterdir()), [])

    def test_tampered_store_is_not_used(self):
        sha = self._action({"action.yml": HELLO})
        workflow = self._workflow(self._uses(f"acme/hello@{sha}"))
        worker = self._start()
        accepted = self._submit("kept", workflow)
        digest = accepted["result"]["input"]["actions"][0]["digest"]
        stored = self.state / "actions" / "objects" / digest / "action.yml"
        stored.write_text(HELLO.replace("remote-ok", "TAMPERED"))
        reused = self._submit("fresh", workflow)
        self.assertNotIn("error", reused, reused)
        self.assertEqual(reused["result"]["input"]["actions"][0]["digest"], digest)
        self.assertEqual(reused["result"]["input"]["actions"][0]["commit"], sha)
        self.assertNotIn("TAMPERED", stored.read_text())
        self.assertIn("remote-ok", stored.read_text())
        snapshot = self.state / "snapshots" / reused["result"]["input"]["snapshot_id"]
        step = plan_snapshot(snapshot, "build", action_store=worker.action_store())["plan"]["job"][
            "steps"
        ][0]
        self.assertNotIn("TAMPERED", step["steps"][0]["run"])
        self.assertEqual(worker.action_store().fetches, 2)
        stored.write_text(HELLO.replace("remote-ok", "TAMPERED"))
        shutil.rmtree(self.remote / "acme")
        refused = self._submit("gone", workflow)
        self.assertEqual(refused["error"]["data"]["kind"], "ACTION_UNAVAILABLE")
        self.assertIsNone(self._row("gone"))
        self.assertIsNotNone(self._row("kept"))
        self.assertGreaterEqual(worker.action_store().fetches, 3)

    def test_node24_main_is_accepted_and_recorded(self):
        node = self._action({"action.yml": NODE24, "index.js": "nope\n"}, repository="nodepin")
        workflow = self._workflow(self._uses(f"acme/nodepin@{node}"))
        worker = self._start()
        accepted = self._submit("node24", workflow)
        self.assertNotIn("error", accepted, accepted)
        result = accepted["result"]
        self.assertEqual(result["state"], "queued")
        recorded = result["input"]["actions"]
        self.assertEqual(recorded[0]["owner"], "acme")
        self.assertEqual(recorded[0]["repository"], "nodepin")
        self.assertEqual(recorded[0]["path"], "")
        self.assertEqual(recorded[0]["commit"], node)
        snapshot = self.state / "snapshots" / result["input"]["snapshot_id"]
        step = plan_snapshot(snapshot, "build", action_store=worker.action_store())["plan"]["job"][
            "steps"
        ][0]
        self.assertEqual(step["javascript"], "node24")
        self.assertEqual(step["main"], "index.js")
        self.assertNotIn("steps", step)
        self.assertEqual(step["outputs"]["answer"], {"description": "Answer"})
        self.assertEqual(worker.action_store().fetches, 1)
        attempt = self.state / "attempts" / "attempt-1"
        staged = stage_node24_actions(
            plan_snapshot(snapshot, "build", action_store=worker.action_store())["plan"],
            worker.action_store(),
            attempt / "actions",
            lambda _size: False,
        )
        copied = staged / "acme" / "nodepin" / node / "index.js"
        self.assertEqual(copied.read_text(), "nope\n")
        self.assertEqual(worker.action_store().fetches, 1)
        stored = self.state / "actions" / "objects" / recorded[0]["digest"] / "index.js"
        copied.write_text("changed\n")
        self.assertEqual(stored.read_text(), "nope\n")
        blocked = self.root / "blocked-actions"
        with self.assertRaises(ActionStorageFull):
            stage_node24_actions(
                plan_snapshot(snapshot, "build", action_store=worker.action_store())["plan"],
                worker.action_store(),
                blocked,
                lambda _size: True,
            )
        self.assertFalse(blocked.exists())
        self.assertEqual(stored.read_text(), "nope\n")
        worker._remove_workspace(attempt)
        self.assertFalse(attempt.exists())
        self.assertTrue(stored.is_file())

    def test_node20_pre_post_and_docker_produce_no_plan(self):
        node20 = self._action({"action.yml": NODE20, "index.js": "nope\n"}, repository="oldpin")
        image = self._action(
            {"action.yml": DOCKER, "Dockerfile": "FROM scratch\n"}, repository="dockpin"
        )
        pre = self._action(
            {"action.yml": PRE, "index.js": "nope\n", "setup.js": "nope\n"}, repository="prepin"
        )
        post = self._action(
            {"action.yml": POST, "index.js": "nope\n", "cleanup.js": "nope\n"},
            repository="postpin",
        )
        missing = self._action(
            {"action.yml": "name: Bare\ndescription: no main\nruns:\n  using: node24\n"},
            repository="barepin",
        )
        self._start()
        cases = (
            ("node20", f"acme/oldpin@{node20}", "CAPABILITY_UNSUPPORTED", "runs.using"),
            ("docker", f"acme/dockpin@{image}", "CAPABILITY_UNSUPPORTED", "runs.using"),
            ("pre", f"acme/prepin@{pre}", "CAPABILITY_UNSUPPORTED", "runs.pre"),
            ("post", f"acme/postpin@{post}", "CAPABILITY_UNSUPPORTED", "runs.post"),
            ("bare", f"acme/barepin@{missing}", "INVALID_PARAMS", "runs.main"),
        )
        for name, pin, kind, field in cases:
            workflow = self._workflow(self._uses(pin), f"{name}.yml")
            reply = self._submit(name, workflow)
            self.assertEqual(reply["error"]["data"]["kind"], kind, reply)
            self.assertIn(field, reply["error"]["message"])
            self.assertIsNone(self._row(name))
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        objects = self.state / "actions" / "objects"
        self.assertFalse(objects.exists() and any(objects.iterdir()))

    def test_fetch_over_budget_returns_storage_full_and_keeps_a_valid_copy(self):
        sha = self._action({"action.yml": HELLO})
        workflow = self._workflow(self._uses(f"acme/hello@{sha}"))
        worker = self._start()
        accepted = self._submit("kept", workflow)
        digest = accepted["result"]["input"]["actions"][0]["digest"]
        stored = self.state / "actions" / "objects" / digest / "action.yml"
        original = stored.read_text()
        worker.disk_budget = 0
        refused = self._submit("over", workflow)
        self.assertEqual(refused["error"]["data"]["kind"], "STORAGE_FULL")
        self.assertIsNone(self._row("over"))
        self.assertEqual(stored.read_text(), original)
        self.assertEqual(worker.action_store().fetches, 1)
        self.assertIsNotNone(self._row("kept"))

    def test_a_refused_fetch_does_not_keep_new_bytes(self):
        sha = self._action({"action.yml": HELLO})
        workflow = self._workflow(self._uses(f"acme/hello@{sha}"))
        worker = self._start()
        worker.disk_budget = 0
        refused = self._submit("over", workflow)
        self.assertEqual(refused["error"]["data"]["kind"], "STORAGE_FULL")
        self.assertIsNone(self._row("over"))
        objects = self.state / "actions" / "objects"
        self.assertFalse(objects.exists() and any(objects.iterdir()))
        self.assertEqual(worker.action_store().fetches, 1)

    def test_checkout_sha_and_local_composite_are_not_fetched(self):
        pin = "a" * 40
        checkout = self._workflow(self._uses(f"actions/checkout@{pin}"), "checkout.yml")
        local = """\
name: Local
description: local
runs:
  using: composite
  steps:
    - shell: bash
      run: printf '%s\\n' local-ok
"""
        action = self.repo / ".github" / "actions" / "hello" / "action.yml"
        action.parent.mkdir(parents=True)
        action.write_text(local)
        workflow = self._workflow(
            "name: demo\non: push\njobs:\n  build:\n    steps:\n"
            "      - uses: ./.github/actions/hello\n",
            "local.yml",
        )
        worker = self._start()
        owned = self._submit("owned", checkout)
        self.assertEqual(owned["result"]["state"], "queued")
        self.assertNotIn("actions", owned["result"]["input"])
        nearby = self._submit("local", workflow)
        self.assertEqual(nearby["result"]["state"], "queued")
        self.assertNotIn("actions", nearby["result"]["input"])
        self.assertEqual(worker.action_store().fetches, 0)

    def test_path_pin_stores_that_directory(self):
        sha = self._action({"action.yml": HELLO, "nested/action.yml": NESTED})
        workflow = self._workflow(self._uses(f"acme/hello/nested@{sha}"))
        worker = self._start()
        accepted = self._submit("path", workflow)
        recorded = accepted["result"]["input"]["actions"][0]
        self.assertEqual(recorded["path"], "nested")
        self.assertEqual(recorded["commit"], sha)
        stored = self.state / "actions" / "objects" / recorded["digest"]
        self.assertIn("nested-ok", (stored / "action.yml").read_text())
        self.assertFalse((stored / "nested").exists())
        snapshot = self.state / "snapshots" / accepted["result"]["input"]["snapshot_id"]
        step = plan_snapshot(snapshot, "build", action_store=worker.action_store())["plan"]["job"][
            "steps"
        ][0]
        self.assertEqual(step["action_path"], "nested")
        self.assertIn("nested-ok", step["steps"][0]["run"])
        self.assertEqual(worker.action_store().fetches, 1)

    def test_escaping_symlink_is_not_stored(self):
        sha = self._action(
            {"action.yml": HELLO, "outside": ("symlink", "/etc/passwd")},
        )
        workflow = self._workflow(self._uses(f"acme/hello@{sha}"))
        worker = self._start()
        refused = self._submit("link", workflow)
        self.assertEqual(refused["error"]["data"]["kind"], "ACTION_UNAVAILABLE")
        self.assertIsNone(self._row("link"))
        objects = self.state / "actions" / "objects"
        self.assertFalse(objects.exists() and any(objects.iterdir()))
        self.assertEqual(worker.action_store().fetches, 1)


def PurePosix(rel):
    return rel.split("/")
