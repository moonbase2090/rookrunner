"""File-backed step env, with, and run. HOME points at a temporary directory.

No test reads the GitHub App key directory or contacts api.github.com.
"""

import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest

from execution_core.expr import (
    ExprError,
    check_step_run,
    check_step_secret_value,
    check_step_text,
    exact_secret_reference,
    rewrite_run_secrets,
)
from execution_core.plan import CAPABILITY_VERSION, PlanError, plan_workflow
from execution_core.run import (
    _JobRuntime,
    _bind_job_secrets,
    _merged_env,
    _publish_script,
    _render_run,
)
from execution_core.secrets import (
    MAX_SECRET_BYTES,
    SecretAccess,
    SecretError,
    load_job_secrets,
)

ROOT = Path(__file__).resolve().parents[1]
PUSH = {
    "ref": "refs/heads/main",
    "repository": {"full_name": "owner/demo", "default_branch": "main"},
}


def _chmod(path, mode):
    os.chmod(path, mode)


def _secret_home(root, files, repository="owner/demo", repo_mode=0o700):
    """Create ~/Secrets/rookrunner-secrets/<owner>/<repo>/ under root."""

    home = root / "home"
    owner, repo = repository.split("/", 1)
    repo_dir = home / "Secrets" / "rookrunner-secrets" / owner / repo
    repo_dir.mkdir(parents=True)
    for path in (
        home,
        home / "Secrets",
        home / "Secrets" / "rookrunner-secrets",
        home / "Secrets" / "rookrunner-secrets" / owner,
        repo_dir,
    ):
        _chmod(path, 0o700)
    _chmod(repo_dir, repo_mode)
    for name, payload in files.items():
        target = repo_dir / name
        if isinstance(payload, tuple) and payload[0] == "link":
            target.symlink_to(payload[1])
            continue
        target.write_bytes(payload)
        _chmod(target, 0o600)
    return home, repo_dir


class SecretFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rr-secrets-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous = os.environ.get("HOME")
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self.previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.previous

    def _use(self, files, **kwargs):
        slot = self.root / f"slot-{len(list(self.root.iterdir()))}"
        slot.mkdir()
        home, repo_dir = _secret_home(slot, files, **kwargs)
        os.environ["HOME"] = str(home)
        return home, repo_dir

    def test_one_trailing_newline_is_stripped(self):
        self._use({"API": b"value\n", "TWO": b"value\n\n", "CR": b"value\r\n"})
        loaded = load_job_secrets("owner/demo", ["API", "TWO", "CR"])
        self.assertEqual(loaded["API"], "value")
        self.assertEqual(loaded["TWO"], "value\n")
        self.assertEqual(loaded["CR"], "value\r")

    def test_lowercase_reference_reads_the_uppercase_file(self):
        self._use({"NPM_TOKEN": b"npm-value\n"})
        loaded = load_job_secrets("owner/demo", ["NPM_TOKEN"])
        self.assertEqual(loaded, {"NPM_TOKEN": "npm-value"})

    def test_missing_name_is_omitted(self):
        self._use({"API": b"value\n"})
        self.assertEqual(load_job_secrets("owner/demo", ["OTHER"]), {})

    def test_unreferenced_bytes_are_not_read(self):
        huge = b"h" * (100 * 1024)
        self._use({"HUGE": huge, "EMPTY": b"", "API": b"\n"})
        self.assertEqual(load_job_secrets("owner/demo", []), {})
        self.assertEqual(load_job_secrets("owner/demo", ["OTHER"]), {})

    def test_referenced_empty_file_names_the_secret(self):
        home, _repo = self._use({"API": b"\n"})
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", ["API"])
        message = str(raised.exception)
        self.assertEqual(message, "secret API is empty")
        self.assertNotIn(str(home), message)
        self.assertNotIn("value", message)

    def test_oversize_and_exact_limit(self):
        exact = b"e" * MAX_SECRET_BYTES
        with_newline = exact + b"\n"
        self._use({"EXACT": exact, "NEWLINE": with_newline})
        loaded = load_job_secrets("owner/demo", ["EXACT", "NEWLINE"])
        self.assertEqual(loaded["EXACT"], "e" * MAX_SECRET_BYTES)
        self.assertEqual(len(loaded["NEWLINE"]), MAX_SECRET_BYTES)
        home, repo = self._use({"BIG": b"b" * (MAX_SECRET_BYTES + 2)})
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", ["BIG"])
        message = str(raised.exception)
        self.assertEqual(message, "secret BIG is not accepted")
        self.assertNotIn(str(home), message)
        self.assertNotIn(str(repo), message)

    def test_nul_and_non_utf8_do_not_echo_bytes(self):
        self._use({"API": b"abc\0def\n"})
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", ["API"])
        message = str(raised.exception)
        self.assertEqual(message, "secret API is not accepted")
        self.assertNotIn("abc", message)
        self.assertNotIn("\0", message)
        self._use({"API": b"\xff\xfe\n"})
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", ["API"])
        self.assertEqual(str(raised.exception), "secret API is not accepted")
        self.assertNotIn("\xff", str(raised.exception))

    def test_bad_mode_symlink_lowercase_and_github_file(self):
        home, repo = self._use({"API": b"value\n"})
        _chmod(repo / "API", 0o644)
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", [])
        self.assertEqual(str(raised.exception), "secret directory is not accepted")
        self.assertNotIn(str(home), str(raised.exception))

        self._use({})
        link = Path(os.environ["HOME"]) / "Secrets/rookrunner-secrets/owner/demo/API"
        link.symlink_to(link.parent / "OTHER")
        (link.parent / "OTHER").write_bytes(b"other-bytes\n")
        _chmod(link.parent / "OTHER", 0o600)
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", ["API"])
        self.assertNotIn("other-bytes", str(raised.exception))
        self.assertNotIn(str(home), str(raised.exception))

        self._use({"api": b"value\n"})
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", [])
        self.assertIn("api", str(raised.exception))
        self.assertNotIn(str(home), str(raised.exception))

        self._use({"GITHUB_TOKEN": b"nope\n"})
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", [])
        self.assertIn("GITHUB_TOKEN", str(raised.exception))
        self.assertNotIn("nope", str(raised.exception))

    def test_second_repository_is_not_read(self):
        home, repo = self._use({"API": b"ours\n"})
        other = repo.parent / "other"
        other.mkdir()
        _chmod(other, 0o700)
        (other / "API").write_bytes(b"theirs\n")
        _chmod(other / "API", 0o600)
        loaded = load_job_secrets("owner/demo", ["API", "ONLY_THERE"])
        self.assertEqual(loaded, {"API": "ours"})
        self.assertNotIn("theirs", loaded.get("API", ""))

        for child in repo.iterdir():
            child.unlink()
        repo.rmdir()
        repo.symlink_to(other, target_is_directory=True)
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", ["API"])
        self.assertEqual(str(raised.exception), "secret directory is not accepted")
        self.assertNotIn("theirs", str(raised.exception))
        self.assertNotIn(str(home), str(raised.exception))

    def test_group_readable_directory_is_refused(self):
        home, _repo = self._use({"API": b"value\n"}, repo_mode=0o707)
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", [])
        self.assertEqual(str(raised.exception), "secret directory is not accepted")
        self.assertNotIn(str(home), str(raised.exception))

    def test_missing_directory_is_refused_without_a_path(self):
        home = self.root / "home"
        home.mkdir()
        _chmod(home, 0o700)
        os.environ["HOME"] = str(home)
        with self.assertRaises(SecretError) as raised:
            load_job_secrets("owner/demo", [])
        self.assertEqual(str(raised.exception), "secret directory is not accepted")
        self.assertNotIn(str(home), str(raised.exception))


class SecretPlanTests(unittest.TestCase):
    def test_exact_step_env_and_with_keep_the_expression(self):
        workflow = """\
on: push
jobs:
  build:
    steps:
      - uses: ./pass
        with:
          token: ${{ secrets.API }}
        env:
          TOKEN: ${{ secrets.npm_token }}
          GITHUB_TOKEN: ${{ secrets.github_token }}
"""
        action = """\
name: pass
description: pass
inputs:
  token:
    description: token
    required: true
runs:
  using: composite
  steps:
    - shell: bash
      run: echo hi
"""
        root = Path(tempfile.mkdtemp(prefix="rr-plan-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        action_dir = root / "pass"
        action_dir.mkdir()
        (action_dir / "action.yml").write_text(action)
        first = plan_workflow(workflow.encode(), "build", action_root=root)
        second = plan_workflow(workflow.encode(), "build", action_root=root)
        self.assertEqual(first["digest"], second["digest"])
        step = first["plan"]["job"]["steps"][0]
        self.assertEqual(step["env"]["TOKEN"], "${{ secrets.npm_token }}")
        self.assertEqual(step["env"]["GITHUB_TOKEN"], "${{ secrets.github_token }}")
        self.assertEqual(step["with"]["token"], "${{ secrets.API }}")
        self.assertEqual(CAPABILITY_VERSION, 12)
        self.assertEqual(first["plan"]["capability_version"], 12)

    def test_mixed_and_github_prefix_are_refused_at_plan_time(self):
        mixed = """\
on: push
jobs:
  build:
    steps:
      - env:
          TOKEN: prefix ${{ secrets.API }}
        run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(mixed.encode(), "build")
        self.assertIn("secrets reference is not accepted", str(raised.exception))
        self.assertIn("env.TOKEN", str(raised.exception))

        prefixed = """\
on: push
jobs:
  build:
    steps:
      - env:
          TOKEN: ${{ secrets.GITHUB_PATH }}
        run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(prefixed.encode(), "build")
        self.assertIn("secrets reference is not accepted", str(raised.exception))
        self.assertIn("env.TOKEN", raised.exception.field or "")

    def test_run_stores_an_exact_secret_and_names_stay_withheld(self):
        run = """\
on: push
jobs:
  build:
    steps:
      - run: echo ${{ secrets.TOKEN }}
"""
        planned = plan_workflow(run.encode(), "build")["plan"]
        self.assertEqual(planned["capability_version"], 12)
        self.assertEqual(planned["job"]["steps"][0]["run"], "echo ${{ secrets.TOKEN }}")

        named = """\
on: push
jobs:
  build:
    steps:
      - name: ${{ secrets.API }}
        run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(named.encode(), "build")
        self.assertIn("context is not available: secrets", str(raised.exception))
        self.assertNotIn("move this reference", str(raised.exception))

    def test_job_and_workflow_env_name_the_move(self):
        workflow = """\
on: push
env:
  TOKEN: ${{ secrets.API }}
jobs:
  build:
    steps:
      - run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), "build")
        self.assertIn("context is not available: secrets", str(raised.exception))
        self.assertIn("move this reference to the step env", str(raised.exception))

        job_env = """\
on: push
jobs:
  build:
    env:
      TOKEN: ${{ secrets.API }}
    steps:
      - run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(job_env.encode(), "build")
        self.assertIn("move this reference to the step env", str(raised.exception))
        self.assertIn("env.TOKEN", raised.exception.field or "")

    def test_rr_secret_key_is_refused_on_step_env_only(self):
        step = """\
on: push
jobs:
  build:
    steps:
      - env:
          RR_SECRET_API: literal
        run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(step.encode(), "build")
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertIn("RR_SECRET_API", raised.exception.field or "")

        job_env = """\
on: push
jobs:
  build:
    env:
      RR_SECRET_API: literal
    steps:
      - run: echo hi
"""
        planned = plan_workflow(job_env.encode(), "build")
        self.assertEqual(planned["plan"]["job"]["env"]["RR_SECRET_API"], "literal")

    def test_composite_inner_env_reserves_the_prefix(self):
        workflow = """\
on: push
jobs:
  build:
    steps:
      - uses: ./pass
"""
        action = """\
name: pass
description: pass
runs:
  using: composite
  steps:
    - shell: bash
      env:
        RR_SECRET_API: literal
      run: echo hi
"""
        root = Path(tempfile.mkdtemp(prefix="rr-plan-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        action_dir = root / "pass"
        action_dir.mkdir()
        (action_dir / "action.yml").write_text(action)
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), "build", action_root=root)
        self.assertIn("RR_SECRET_API", raised.exception.field or "")

    def test_write_scopes_stay_rejected(self):
        for scope in ("security-events", "actions"):
            workflow = f"""\
on: push
permissions:
  {scope}: write
jobs:
  build:
    steps:
      - run: echo hi
"""
            with self.assertRaises(PlanError) as raised:
                plan_workflow(workflow.encode(), "build")
            self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")

    def test_check_yml_has_no_secret_flag(self):
        text = (ROOT / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
        self.assertNotIn("--secrets", text)
        self.assertNotIn("--app-key", text)
        self.assertNotIn("github-app", text)
        self.assertNotIn("private-key", text)
        self.assertEqual(CAPABILITY_VERSION, 12)

    def test_exact_reference_helper(self):
        self.assertEqual(exact_secret_reference("${{ secrets.npm_token }}"), "npm_token")
        self.assertEqual(exact_secret_reference("${{ secrets.GITHUB_TOKEN }}"), "GITHUB_TOKEN")
        self.assertIsNone(exact_secret_reference("prefix ${{ secrets.API }}"))
        self.assertIsNone(exact_secret_reference("${{ secrets.API }}_v2"))
        check_step_secret_value("${{ secrets.API }}")
        check_step_secret_value("${{ github.token }}")
        with self.assertRaises(Exception) as raised:
            check_step_secret_value("echo ${{ secrets.API }}")
        self.assertIn("secrets reference is not accepted", str(raised.exception))
        with self.assertRaises(Exception) as raised:
            check_step_text("echo ${{ secrets.TOKEN }}")
        self.assertIn("context is not available: secrets", str(raised.exception))


class SecretBindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rr-bind-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous = os.environ.get("HOME")
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self.previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.previous

    def _job(self, expressions):
        return {"steps": [{"env": expressions, "with": {}}]}

    def test_matching_push_registers_the_value_and_names_a_miss(self):
        home, _repo = _secret_home(
            self.root, {"API": b"fixture-secret-value-9f3a\n", "SHORT": b"ab\n"}
        )
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        job = self._job(
            {
                "TOKEN": "${{ secrets.API }}",
                "LITTLE": "${{ secrets.short }}",
                "GONE": "${{ secrets.MISSING }}",
                "TOKEN_ENV": "${{ secrets.GITHUB_TOKEN }}",
            }
        )
        _bind_job_secrets(runtime, SecretAccess("owner/demo"), PUSH, "push", job)
        self.assertEqual(runtime.secret_values["API"], "fixture-secret-value-9f3a")
        self.assertEqual(runtime.secret_values["SHORT"], "ab")
        self.assertEqual(runtime.secret_values["MISSING"], "")
        self.assertNotIn("GITHUB_TOKEN", runtime.secret_values)
        self.assertIn("fixture-secret-value-9f3a", runtime.masks.secrets)
        self.assertIn("ab", runtime.masks.secrets)
        self.assertNotIn("", runtime.masks.secrets)
        self.assertIn("secret MISSING is not set", runtime.secret_notes)
        self.assertTrue(any("SHORT" in item for item in runtime.masks.warnings))
        self.assertFalse(
            any("fixture-secret-value-9f3a" in item for item in runtime.masks.warnings)
        )
        self.assertNotIn(str(home), " ".join(runtime.secret_notes))

    def test_allowlist_miss_does_not_open_a_loose_directory(self):
        home, _repo = _secret_home(self.root, {"API": b"value\n"}, repo_mode=0o777)
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        event = {
            "ref": "refs/heads/feature",
            "repository": {"full_name": "owner/demo", "default_branch": "main"},
        }
        _bind_job_secrets(
            runtime,
            SecretAccess("owner/demo"),
            event,
            "push",
            self._job({"TOKEN": "${{ secrets.API }}"}),
        )
        self.assertEqual(runtime.secret_values, {})
        self.assertEqual(runtime.secret_notes, [])
        self.assertEqual(runtime.masks.secrets, [])

    def test_repository_mismatch_does_not_open_the_directory(self):
        home, _repo = _secret_home(self.root, {"API": b"value\n"}, repo_mode=0o777)
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        event = {
            "ref": "refs/heads/main",
            "repository": {"full_name": "other/demo", "default_branch": "main"},
        }
        with self.assertRaises(SecretError) as raised:
            _bind_job_secrets(
                runtime,
                SecretAccess("owner/demo"),
                event,
                "push",
                self._job({"TOKEN": "${{ secrets.API }}"}),
            )
        self.assertEqual(str(raised.exception), "repository does not match the secret repository")
        self.assertNotIn(str(home), str(raised.exception))

    def test_omitted_config_does_not_open(self):
        home, _repo = _secret_home(self.root, {"API": b"value\n"}, repo_mode=0o777)
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        _bind_job_secrets(
            runtime,
            None,
            PUSH,
            "push",
            self._job({"TOKEN": "${{ secrets.API }}"}),
        )
        self.assertIsNone(runtime.secret_values)

    def test_github_token_does_not_read_an_unreferenced_file(self):
        huge = b"h" * (100 * 1024)
        home, _repo = _secret_home(self.root, {"API": huge})
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        _bind_job_secrets(
            runtime,
            SecretAccess("owner/demo"),
            PUSH,
            "push",
            self._job({"TOKEN": "${{ secrets.GITHUB_TOKEN }}"}),
        )
        self.assertEqual(runtime.secret_values, {})
        self.assertEqual(hashlib.sha256(huge).hexdigest(), hashlib.sha256(huge).hexdigest())

    def test_no_reference_still_checks_the_directory(self):
        home = self.root / "home"
        home.mkdir()
        _chmod(home, 0o700)
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        with self.assertRaises(SecretError) as raised:
            _bind_job_secrets(
                runtime,
                SecretAccess("owner/demo"),
                PUSH,
                "push",
                {"steps": [{"run": "echo hi"}]},
            )
        self.assertEqual(str(raised.exception), "secret directory is not accepted")
        self.assertNotIn(str(home), str(raised.exception))

    def test_run_reference_loads_masks_and_names_a_miss(self):
        home, _repo = _secret_home(self.root, {"API": b"fixture-run-value-9f3a\n"})
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        _bind_job_secrets(
            runtime,
            SecretAccess("owner/demo"),
            PUSH,
            "push",
            {"steps": [{"run": 'echo "${{ secrets.API }}_v2 ${{ secrets.MISSING }}"'}]},
        )
        self.assertEqual(runtime.secret_values["API"], "fixture-run-value-9f3a")
        self.assertEqual(runtime.secret_values["MISSING"], "")
        self.assertNotIn("GITHUB_TOKEN", runtime.secret_values)
        self.assertIn("fixture-run-value-9f3a", runtime.masks.secrets)
        self.assertIn("secret MISSING is not set", runtime.secret_notes)
        self.assertNotIn(str(home), " ".join(runtime.secret_notes))

    def test_run_github_token_does_not_read_a_file(self):
        huge = b"h" * (100 * 1024)
        home, _repo = _secret_home(self.root, {"API": huge})
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        _bind_job_secrets(
            runtime,
            SecretAccess("owner/demo"),
            PUSH,
            "push",
            {"steps": [{"run": "echo ${{ secrets.GITHUB_TOKEN }}"}]},
        )
        self.assertEqual(runtime.secret_values, {})

    def test_run_empty_file_is_refused_before_a_value_is_returned(self):
        home, _repo = _secret_home(self.root, {"API": b"\n"})
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        with self.assertRaises(SecretError) as raised:
            _bind_job_secrets(
                runtime,
                SecretAccess("owner/demo"),
                PUSH,
                "push",
                {"steps": [{"run": "echo ${{ secrets.API }}"}]},
            )
        self.assertEqual(str(raised.exception), "secret API is empty")
        self.assertNotIn(str(home), str(raised.exception))

    def test_run_allowlist_miss_does_not_open_a_loose_directory(self):
        home, _repo = _secret_home(self.root, {"API": b"value\n"}, repo_mode=0o777)
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        event = {
            "ref": "refs/heads/feature",
            "repository": {"full_name": "owner/demo", "default_branch": "main"},
        }
        _bind_job_secrets(
            runtime,
            SecretAccess("owner/demo"),
            event,
            "push",
            {"steps": [{"run": "echo ${{ secrets.API }}"}]},
        )
        self.assertEqual(runtime.secret_values, {})
        self.assertEqual(runtime.secret_notes, [])

    def test_run_omitted_config_does_not_open(self):
        home, _repo = _secret_home(self.root, {"API": b"value\n"}, repo_mode=0o777)
        os.environ["HOME"] = str(home)
        runtime = _JobRuntime()
        _bind_job_secrets(
            runtime,
            None,
            PUSH,
            "push",
            {"steps": [{"run": "echo ${{ secrets.API }}"}]},
        )
        self.assertIsNone(runtime.secret_values)


class SecretModeTests(unittest.TestCase):
    def test_directory_mode_constant(self):
        self.assertEqual(stat.S_IMODE(0o100700), 0o700)
        self.assertEqual(MAX_SECRET_BYTES, 48 * 1024)


class RunRewriteTests(unittest.TestCase):
    def test_proven_contexts_rewrite_to_the_reserved_name(self):
        cases = {
            "echo ${{ secrets.API }}": ("echo ${RR_SECRET_API}", ["API"]),
            "echo ${{ secrets.API }}_v2": ("echo ${RR_SECRET_API}_v2", ["API"]),
            'echo "${{ secrets.API }}"': ('echo "${RR_SECRET_API}"', ["API"]),
            "echo ${{ secrets.npm_token }}": ("echo ${RR_SECRET_NPM_TOKEN}", ["NPM_TOKEN"]),
            "echo ${{ secrets.API }}${{ secrets.OTHER }}": (
                "echo ${RR_SECRET_API}${RR_SECRET_OTHER}",
                ["API", "OTHER"],
            ),
            "echo ${{ secrets.API }} ${{ github.sha }}": (
                "echo ${RR_SECRET_API} ${{ github.sha }}",
                ["API"],
            ),
            "echo ${{ secrets.GITHUB_TOKEN }}": ("echo ${RR_SECRET_GITHUB_TOKEN}", []),
            "echo  ${{ secrets.API }}": ("echo  ${RR_SECRET_API}", ["API"]),
            "echo $(echo ${{ secrets.API }})": ("echo $(echo ${RR_SECRET_API})", ["API"]),
            'echo "$(echo ${{ secrets.API }})"': (
                'echo "$(echo ${RR_SECRET_API})"',
                ["API"],
            ),
            "echo <<< ${{ secrets.API }}": ("echo <<< ${RR_SECRET_API}", ["API"]),
            "echo ${var:-x} ${{ secrets.API }}": (
                "echo ${var:-x} ${RR_SECRET_API}",
                ["API"],
            ),
            "echo ${var:-$(echo ${{ secrets.API }})}": (
                "echo ${var:-$(echo ${RR_SECRET_API})}",
                ["API"],
            ),
            "cat <<'EOF'\nbody\nEOF\necho ${{ secrets.API }}\n": (
                "cat <<'EOF'\nbody\nEOF\necho ${RR_SECRET_API}\n",
                ["API"],
            ),
            "cat <<EOF $(echo x)\nEOF\necho ${{ secrets.API }}\n": (
                "cat <<EOF $(echo x)\nEOF\necho ${RR_SECRET_API}\n",
                ["API"],
            ),
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(rewrite_run_secrets(source, None), expected)
                check_step_run(source, None)

    def test_unproven_contexts_and_other_shells_name_run(self):
        refused = [
            "echo '${{ secrets.API }}'",
            "echo $'${{ secrets.API }}'",
            "echo $'\\'${{ secrets.API }}'",
            "echo hi # ${{ secrets.API }}",
            "cat <<'EOF'\n${{ secrets.API }}\nEOF\n",
            "cat <<EOF\n${{ secrets.API }}\nEOF\n",
            "cat <<EOF $(echo x)\n${{ secrets.API }}\nEOF\n",
            "echo $(cat <<EOF)\n${{ secrets.API }}\nEOF\n",
            'echo $"${{ secrets.API }}"',
            "echo ${var:-${{ secrets.API }}}",
            "echo ${var:-'${{ secrets.API }}'}",
            "echo $(( ${{ secrets.API }} ))",
            "echo \\${{ secrets.API }}",
            'echo "${{ secrets.API }}',
            "echo \"$(echo '${{ secrets.API }}')\"",
        ]
        for source in refused:
            with self.subTest(source=source):
                with self.assertRaises(ExprError) as raised:
                    rewrite_run_secrets(source, "bash")
                self.assertEqual(str(raised.exception), "run is not accepted")
        for shell in ("pwsh", "python {0}", "/bin/bash", "bash ", "Bash", "", "bash {0} {0}"):
            with self.subTest(shell=shell):
                with self.assertRaises(ExprError) as raised:
                    rewrite_run_secrets("echo ${{ secrets.API }}", shell)
                self.assertEqual(str(raised.exception), "run is not accepted")
        for shell in (
            "bash",
            "sh",
            "bash -e {0}",
            "sh -e {0}",
            "bash --noprofile --norc -eo pipefail {0}",
        ):
            with self.subTest(shell=shell):
                text, names = rewrite_run_secrets("echo ${{ secrets.API }}", shell)
                self.assertEqual(text, "echo ${RR_SECRET_API}")
                self.assertEqual(names, ["API"])
        self.assertEqual(rewrite_run_secrets("echo hi", "pwsh"), ("echo hi", []))

    def test_other_secret_reads_stay_refused(self):
        for source in (
            "echo ${{ secrets.API || 'x' }}",
            "echo ${{ secrets['API'] }}",
            "echo ${{ secrets.GITHUB_PATH }}",
        ):
            with self.subTest(source=source):
                with self.assertRaises(ExprError) as raised:
                    rewrite_run_secrets(source, None)
                self.assertEqual(str(raised.exception), "secrets reference is not accepted")

    def test_plan_refuses_a_bad_shell_and_keeps_a_bash_override(self):
        refused = """\
on: push
defaults:
  run:
    shell: pwsh
jobs:
  build:
    steps:
      - run: echo ${{ secrets.API }}
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(refused.encode(), "build")
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertTrue((raised.exception.field or "").endswith(".run"))
        self.assertIn("run is not accepted", str(raised.exception))

        overridden = """\
on: push
defaults:
  run:
    shell: pwsh
jobs:
  build:
    defaults:
      run:
        shell: bash
    steps:
      - run: echo ${{ secrets.API }}
"""
        planned = plan_workflow(overridden.encode(), "build")["plan"]
        self.assertEqual(planned["job"]["steps"][0]["run"], "echo ${{ secrets.API }}")

        step_shell = """\
on: push
defaults:
  run:
    shell: pwsh
jobs:
  build:
    steps:
      - shell: bash
        run: echo ${{ secrets.API }}
"""
        planned = plan_workflow(step_shell.encode(), "build")["plan"]
        self.assertEqual(planned["job"]["steps"][0]["run"], "echo ${{ secrets.API }}")

        plain = """\
on: push
jobs:
  build:
    steps:
      - shell: pwsh
        run: echo hi
"""
        planned = plan_workflow(plain.encode(), "build")["plan"]
        self.assertEqual(planned["job"]["steps"][0]["run"], "echo hi")

    def test_composite_uses_its_own_shell(self):
        workflow = """\
on: push
jobs:
  build:
    steps:
      - uses: ./pass
"""
        bash = """\
name: pass
description: pass
runs:
  using: composite
  steps:
    - shell: bash
      run: echo ${{ secrets.API }}
"""
        root = Path(tempfile.mkdtemp(prefix="rr-run-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        action = root / "pass"
        action.mkdir()
        (action / "action.yml").write_text(bash)
        planned = plan_workflow(workflow.encode(), "build", action_root=root)["plan"]
        self.assertEqual(
            planned["job"]["steps"][0]["steps"][0]["run"],
            "echo ${{ secrets.API }}",
        )
        (action / "action.yml").write_text(bash.replace("shell: bash", "shell: pwsh"))
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), "build", action_root=root)
        self.assertIn("run is not accepted", str(raised.exception))
        self.assertTrue((raised.exception.field or "").endswith(".run"))

    def test_called_workflow_uses_its_own_shell(self):
        caller = """\
on: push
defaults:
  run:
    shell: pwsh
jobs:
  call:
    uses: ./.github/workflows/called.yml
"""
        called = """\
on: workflow_call
defaults:
  run:
    shell: bash
jobs:
  build:
    steps:
      - run: echo ${{ secrets.API }}
"""
        root = Path(tempfile.mkdtemp(prefix="rr-call-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        path = root / ".github" / "workflows" / "called.yml"
        path.parent.mkdir(parents=True)
        path.write_text(called)
        planned = plan_workflow(caller.encode(), "call", action_root=root)["plan"]
        self.assertEqual(
            planned["job"]["call"]["jobs"][0]["steps"][0]["run"],
            "echo ${{ secrets.API }}",
        )
        path.write_text(called.replace("shell: bash", "shell: pwsh"))
        caller_bash = caller.replace("shell: pwsh", "shell: bash")
        with self.assertRaises(PlanError) as raised:
            plan_workflow(caller_bash.encode(), "call", action_root=root)
        self.assertIn("run is not accepted", str(raised.exception))

    def test_publish_and_render_keep_the_value_out_of_the_script(self):
        commands = Path(tempfile.mkdtemp(prefix="rr-cmd-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(commands))
        mounted = _publish_script(commands, "step-0", "echo ${RR_SECRET_API}_v2\n")
        self.assertEqual(mounted, "/run/rookrunner-cmd/step-0-script")
        body = (commands / "step-0-script").read_text(encoding="utf-8")
        self.assertIn("${RR_SECRET_API}", body)
        self.assertNotIn("fixture", body)

        runtime = _JobRuntime()
        with self.assertRaises(ExprError) as raised:
            _render_run("echo ${{ secrets.API }}", {}, None, runtime)
        self.assertEqual(str(raised.exception), "context is not available: secrets")

        runtime.secret_values = {}
        text, env = _render_run("echo ${{ secrets.API }}_v2", {}, None, runtime)
        self.assertEqual(text, "echo ${RR_SECRET_API}_v2")
        self.assertEqual(env, {"API": ""})
        text, env = _render_run("echo ${{ secrets.GITHUB_TOKEN }}", {}, None, runtime)
        self.assertEqual(text, "echo ${RR_SECRET_GITHUB_TOKEN}")
        self.assertEqual(env, {})

        runtime.secret_values = {"API": 'fixture run "9f3a"'}
        text, env = _render_run(
            "echo ${{ secrets.API }} ${{ github.sha }}",
            {"github": {"sha": "abc123"}},
            "bash",
            runtime,
        )
        self.assertEqual(text, "echo ${RR_SECRET_API} abc123")
        self.assertEqual(env["API"], 'fixture run "9f3a"')
        self.assertNotIn("fixture", text)

        step = {"env": {}, "_rr_secrets": {"API": 'fixture run "9f3a"'}}
        other = {"env": {}}
        job = {"env": {"RR_SECRET_API": "from-job"}}
        found = dict(_merged_env({"env": {}}, job, step, None, "step-0", None))
        self.assertEqual(found["RR_SECRET_API"], 'fixture run "9f3a"')
        plain = dict(_merged_env({"env": {}}, {"env": {}}, other, None, "step-1", None))
        self.assertNotIn("RR_SECRET_API", plain)
