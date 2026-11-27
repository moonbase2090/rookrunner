# SPDX-License-Identifier: MIT

"""The package cache: who may use it, how it is kept, and what a job sees."""

import ast
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from execution_core.disk import usage
from execution_core.package_cache import (
    BRANCH_NAMESPACE,
    CACHE_ROOT,
    CARGO_TOOLS_ROOT,
    DEFAULT_BRANCH_NAMESPACE,
    TOOLS_BIN,
    CacheMount,
    PackageCache,
    image_key,
    namespace_for,
    repository_from_clone,
)
from execution_core.run import _create_args, run_job
from execution_core.worker import Worker

from test_run import EVENT, _capture, _plan

REPOSITORY = "owner/project"
IMAGE_A = "a" * 16
IMAGE_B = "b" * 16


def _repository(default="main"):
    return {"id": 77, "full_name": REPOSITORY, "default_branch": default}


def _push(ref):
    return {"ref": ref, "repository": _repository(), "after": "a" * 40}


def _pull(head_id=77, base_id=77):
    return {
        "ref": "refs/pull/9/merge",
        "repository": _repository(),
        "pull_request": {
            "head": {"sha": "a" * 40, "ref": "topic", "repo": {"id": head_id}},
            "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": base_id}},
        },
    }


class NamespaceTests(unittest.TestCase):
    def test_a_push_to_the_default_branch_uses_its_own_namespace(self):
        self.assertEqual(
            namespace_for(_push("refs/heads/main"), "push", REPOSITORY), DEFAULT_BRANCH_NAMESPACE
        )

    def test_a_push_to_another_branch_shares_with_pull_requests_of_the_repository(self):
        self.assertEqual(
            namespace_for(_push("refs/heads/topic"), "push", REPOSITORY), BRANCH_NAMESPACE
        )
        self.assertEqual(namespace_for(_pull(), "pull_request", REPOSITORY), BRANCH_NAMESPACE)

    def test_default_branch_and_branch_work_never_share_a_namespace(self):
        main = namespace_for(_push("refs/heads/main"), "push", REPOSITORY)
        topic = namespace_for(_push("refs/heads/topic"), "push", REPOSITORY)
        pull = namespace_for(_pull(), "pull_request", REPOSITORY)
        self.assertNotEqual(main, topic)
        self.assertNotEqual(main, pull)

    def test_a_pull_request_from_a_fork_gets_no_cache(self):
        self.assertIsNone(namespace_for(_pull(head_id=900), "pull_request", REPOSITORY))

    def test_a_pull_request_whose_base_is_not_this_repository_gets_no_cache(self):
        self.assertIsNone(namespace_for(_pull(base_id=900), "pull_request", REPOSITORY))

    def test_a_pull_request_without_a_head_repository_gets_no_cache(self):
        event = _pull()
        event["pull_request"]["head"]["repo"] = None
        self.assertIsNone(namespace_for(event, "pull_request", REPOSITORY))

    def test_an_untrusted_override_gets_no_cache_on_any_event(self):
        self.assertIsNone(
            namespace_for(_push("refs/heads/main"), "push", REPOSITORY, untrusted_override=True)
        )
        self.assertIsNone(namespace_for(_pull(), "pull_request", REPOSITORY, True))

    def test_an_event_for_another_repository_gets_no_cache(self):
        self.assertIsNone(namespace_for(_push("refs/heads/main"), "push", "owner/other"))

    def test_a_tag_and_an_unknown_event_get_no_cache(self):
        self.assertIsNone(namespace_for(_push("refs/tags/v1"), "push", REPOSITORY))
        self.assertIsNone(namespace_for(_push("refs/heads/main"), "issues", REPOSITORY))
        self.assertIsNone(namespace_for({"repository": _repository()}, "push", REPOSITORY))
        self.assertIsNone(namespace_for("not an event", "push", REPOSITORY))

    def test_a_scheduled_run_is_trusted_only_on_the_default_branch(self):
        self.assertEqual(
            namespace_for(_push("refs/heads/main"), "schedule", REPOSITORY),
            DEFAULT_BRANCH_NAMESPACE,
        )
        self.assertIsNone(namespace_for(_push("refs/heads/topic"), "schedule", REPOSITORY))

    def test_a_worker_without_a_verified_repository_gets_no_cache(self):
        self.assertIsNone(namespace_for(_push("refs/heads/main"), "push", None))
        self.assertIsNone(namespace_for(_pull(), "pull_request", None))
        self.assertIsNone(namespace_for(_pull(), "pull_request", ""))

    def test_an_event_missing_the_repository_id_or_the_default_branch_gets_no_cache(self):
        no_id = _push("refs/heads/main")
        del no_id["repository"]["id"]
        self.assertIsNone(namespace_for(no_id, "push", REPOSITORY))
        no_default = _push("refs/heads/topic")
        del no_default["repository"]["default_branch"]
        self.assertIsNone(namespace_for(no_default, "push", REPOSITORY))
        empty_default = {**_push("refs/heads/main"), "repository": _repository(default="")}
        self.assertIsNone(namespace_for(empty_default, "push", REPOSITORY))
        no_pull_id = _pull()
        del no_pull_id["repository"]["id"]
        self.assertIsNone(namespace_for(no_pull_id, "pull_request", REPOSITORY))

    def test_a_ref_listed_for_secrets_gets_no_cache_while_other_branches_keep_theirs(self):
        listed = ("refs/heads/release/1",)
        self.assertIsNone(
            namespace_for(_push("refs/heads/release/1"), "push", REPOSITORY, secret_refs=listed)
        )
        self.assertEqual(
            namespace_for(_push("refs/heads/topic"), "push", REPOSITORY, secret_refs=listed),
            BRANCH_NAMESPACE,
        )
        self.assertEqual(
            namespace_for(_push("refs/heads/main"), "push", REPOSITORY, secret_refs=listed),
            DEFAULT_BRANCH_NAMESPACE,
        )
        self.assertEqual(
            namespace_for(_pull(), "pull_request", REPOSITORY, secret_refs=listed),
            BRANCH_NAMESPACE,
        )

    def test_the_repository_comes_from_the_clone_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory)
            subprocess.run(["git", "init", "-q", str(clone)], check=True)
            self.assertIsNone(repository_from_clone(clone))
            for url, name in (
                ("https://github.com/owner/project.git", "owner/project"),
                ("https://github.com/owner/project", "owner/project"),
                ("git@github.com:owner/project.git", "owner/project"),
                ("not a url", None),
            ):
                subprocess.run(
                    ["git", "-C", str(clone), "remote", "remove", "origin"], capture_output=True
                )
                subprocess.run(
                    ["git", "-C", str(clone), "remote", "add", "origin", url], check=True
                )
                self.assertEqual(repository_from_clone(clone), name, url)

    def test_the_image_key_is_the_front_of_a_sha256_digest(self):
        self.assertEqual(image_key("sha256:" + "0123456789abcdef" + "f" * 48), "0123456789abcdef")
        for bad in (None, "abc", "sha256:xyz", "sha256:" + "A" * 64, "sha256:12"):
            self.assertIsNone(image_key(bad))


class KeepingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.now = 1_000_000.0
        self.cache = PackageCache(
            self.state, max_age_seconds=100, max_bytes=1_000_000, clock=lambda: self.now
        )

    def test_a_cache_is_private_and_holds_the_pieces_a_job_mounts(self):
        mount = self.cache.acquire(DEFAULT_BRANCH_NAMESPACE, IMAGE_A, 77)
        for path in (mount.root, mount.registry, mount.git, mount.shared):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700, path)
        for name in (
            "pip",
            "uv",
            "npm",
            "pnpm-store",
            "yarn",
            "go-mod",
            "cargo-tools",
            "tools/bin",
            "tools/pipx",
            "tools/uv",
        ):
            self.assertTrue((mount.shared / name).is_dir(), name)
        self.assertEqual(
            [lock.name for lock in mount.locks], [".package-cache", ".package-cache-mutate"]
        )
        for lock in mount.locks:
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_caches_of_different_namespaces_or_images_share_no_path(self):
        paths = {
            self.cache.acquire(namespace, image, 77).root
            for namespace in (DEFAULT_BRANCH_NAMESPACE, BRANCH_NAMESPACE)
            for image in (IMAGE_A, IMAGE_B)
        }
        self.assertEqual(len(paths), 4)

    def test_the_mounts_put_cargos_parts_under_the_image_cargo_home_and_the_rest_in_one_mount(self):
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        destinations = {
            part.split("destination=")[1]
            for part in mount.mount_args("/usr/local/cargo")
            if part.startswith("type=bind")
        }
        self.assertEqual(
            destinations,
            {
                CACHE_ROOT,
                "/usr/local/cargo/registry",
                "/usr/local/cargo/git",
                "/usr/local/cargo/.package-cache",
                "/usr/local/cargo/.package-cache-mutate",
            },
        )
        only = mount.mount_args(None)
        self.assertEqual(only.count("--mount"), 1)
        self.assertIn(f"destination={CACHE_ROOT}", only[1])

    def test_the_default_variables_point_every_package_manager_at_the_mount(self):
        env = CacheMount.env_defaults()
        for name in (
            "PIP_CACHE_DIR",
            "UV_CACHE_DIR",
            "npm_config_cache",
            "npm_config_store_dir",
            "YARN_CACHE_FOLDER",
            "GOMODCACHE",
            "CARGO_INSTALL_ROOT",
            "PIPX_HOME",
            "UV_TOOL_DIR",
        ):
            self.assertTrue(env[name].startswith(CACHE_ROOT + "/"), name)
        self.assertEqual(env["UV_LINK_MODE"], "copy")
        self.assertEqual(env["npm_config_package_import_method"], "copy")
        self.assertNotIn("GOCACHE", env)
        self.assertEqual(CacheMount.path_prefixes(), [f"{CARGO_TOOLS_ROOT}/bin", TOOLS_BIN])

    def test_cached_files_survive_between_jobs(self):
        first = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        (first.registry / "crate").write_text("bytes")
        self.cache.release(first)
        second = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertEqual((second.registry / "crate").read_text(), "bytes")

    def test_a_cache_idle_for_the_maximum_age_is_removed_and_a_used_one_is_not(self):
        idle = self.cache.acquire(DEFAULT_BRANCH_NAMESPACE, IMAGE_A, 77)
        self.cache.release(idle)
        busy = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.now += 99
        self.assertEqual(self.cache.prune(), [])
        self.now += 1
        self.assertEqual(self.cache.prune(), [idle.root])
        self.assertFalse(idle.root.exists())
        self.assertTrue(busy.root.exists())

    def test_one_image_ageing_out_leaves_the_other_image_of_the_same_namespace(self):
        old = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.cache.release(old)
        self.now += 60
        fresh = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_B, 77)
        self.cache.release(fresh)
        self.now += 60
        self.assertEqual(self.cache.prune(), [old.root])
        self.assertTrue(fresh.root.exists())

    def test_using_a_cache_restarts_its_age(self):
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.cache.release(mount)
        self.now += 90
        again = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.cache.release(again)
        self.now += 90
        self.assertEqual(self.cache.prune(), [])
        self.assertTrue(mount.root.exists())

    def test_reclaiming_frees_the_idle_cache_used_longest_ago_and_skips_busy_ones(self):
        old = self.cache.acquire(DEFAULT_BRANCH_NAMESPACE, IMAGE_A, 77)
        self.cache.release(old)
        self.now += 10
        recent = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.cache.release(recent)
        self.assertEqual(self.cache.reclaim(), old.root)
        self.assertTrue(recent.root.exists())
        busy = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertIsNone(self.cache.reclaim())
        self.assertTrue(busy.root.exists())

    def test_an_idle_cache_over_the_size_limit_starts_empty_but_a_busy_one_is_kept(self):
        first = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        (first.registry / "big").write_bytes(b"x" * 2_000_000)
        second = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertTrue((second.registry / "big").exists())
        self.cache.release(first)
        self.cache.release(second)
        third = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertFalse((third.registry / "big").exists())
        self.assertTrue(third.registry.is_dir())

    def test_a_read_only_go_module_cache_is_still_removed(self):
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        module = mount.shared / "go-mod" / "example.com" / "dep@v1.0.0"
        module.mkdir(parents=True)
        (module / "dep.go").write_text("package dep\n")
        (module / "dep.go").chmod(0o444)
        module.chmod(0o555)
        module.parent.chmod(0o555)
        self.cache.release(mount)
        self.now += 100
        self.assertEqual(self.cache.prune(), [mount.root])
        self.assertFalse(mount.root.exists())

    def test_a_symlinked_namespace_is_replaced_by_a_real_directory_never_followed(self):
        target = self.state / "elsewhere"
        target.mkdir()
        (target / "kept.txt").write_text("keep\n")
        (self.state / "package-cache").mkdir()
        (self.state / "package-cache" / BRANCH_NAMESPACE).symlink_to(target)
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertFalse((self.state / "package-cache" / BRANCH_NAMESPACE).is_symlink())
        self.assertTrue(mount.registry.is_dir())
        self.assertEqual([item.name for item in target.iterdir()], ["kept.txt"])

    def test_a_symlink_where_the_cache_expects_a_directory_or_a_lock_is_refused_untouched(self):
        outside = self.state / "outside"
        outside.mkdir()
        outside.chmod(0o755)
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.cache.release(mount)
        for name in ("git", ".package-cache"):
            path = mount.root / name
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            path.symlink_to(outside)
            with self.assertRaises(OSError):
                self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o755, name)
            path.unlink()
            self.cache.release(self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77))
            self.cache.release(mount)

    def test_removal_never_follows_a_symlink_out_of_the_cache(self):
        outside = self.state / "outside"
        outside.mkdir()
        (outside / "kept.txt").write_text("keep\n")
        (outside / "kept.txt").chmod(0o644)
        outside.chmod(0o755)
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        (mount.registry / "to-dir").symlink_to(outside)
        (mount.registry / "to-file").symlink_to(outside / "kept.txt")
        (mount.registry / "dangling").symlink_to(self.state / "does-not-exist")
        nested = mount.shared / "go-mod" / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "link").symlink_to(outside)
        (nested / "file").write_text("x")
        nested.chmod(0o555)
        self.cache.release(mount)
        self.now += 100
        self.assertEqual(self.cache.prune(), [mount.root])
        self.assertFalse(mount.root.exists())
        self.assertEqual((outside / "kept.txt").read_text(), "keep\n")
        self.assertEqual((outside / "kept.txt").stat().st_mode & 0o777, 0o644)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    def test_a_symlink_in_any_ancestor_is_refused_before_anything_is_measured_or_removed(self):
        outside = self.state / "outside"
        (outside / BRANCH_NAMESPACE / IMAGE_A).mkdir(parents=True)
        big = outside / BRANCH_NAMESPACE / IMAGE_A / "big"
        big.write_bytes(b"x" * 2_000_000)
        outside.chmod(0o755)
        # The cache root itself is a link out of the state directory.
        (self.state / "package-cache").symlink_to(outside)
        with self.assertRaises(OSError):
            self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertEqual(self.cache.prune(), [])
        self.assertIsNone(self.cache.reclaim())
        self.assertTrue(big.exists())
        (self.state / "package-cache").unlink()
        # A link below the cache root is unlinked, and what it pointed at is
        # neither measured, emptied nor removed.
        (self.state / "package-cache").mkdir()
        (self.state / "package-cache" / BRANCH_NAMESPACE).symlink_to(outside / BRANCH_NAMESPACE)
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertTrue(big.exists())
        self.assertFalse((self.state / "package-cache" / BRANCH_NAMESPACE).is_symlink())
        self.assertTrue(mount.registry.is_dir())
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    def test_a_link_planted_where_a_cache_is_measured_is_not_followed(self):
        outside = self.state / "outside"
        outside.mkdir()
        (outside / "huge").write_bytes(b"x" * 5_000_000)
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.cache.release(mount)
        (mount.registry / "link").symlink_to(outside)
        again = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertTrue((again.registry / "link").is_symlink())
        self.assertTrue((outside / "huge").exists())

    def test_a_cache_filled_for_one_repository_is_emptied_for_another(self):
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        (mount.registry / "crate").write_text("bytes")
        self.cache.release(mount)
        same = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertEqual((same.registry / "crate").read_text(), "bytes")
        self.cache.release(same)
        other = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 78)
        self.assertFalse((other.registry / "crate").exists())
        self.assertEqual((self.state / "package-cache" / "repository").read_text(), "78\n")
        self.cache.release(other)
        back = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertFalse((back.registry / "crate").exists())

    def test_a_cache_in_use_is_not_emptied_for_another_repository(self):
        busy = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        (busy.registry / "crate").write_text("bytes")
        with self.assertRaises(OSError):
            self.cache.acquire(BRANCH_NAMESPACE, IMAGE_B, 78)
        self.assertEqual((busy.registry / "crate").read_text(), "bytes")

    def test_a_repository_id_must_be_a_positive_integer(self):
        for bad in (None, 0, -1, True, "77", 1.5):
            with self.assertRaises(ValueError):
                self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, bad)

    def test_a_scheduled_or_dispatched_event_without_the_repository_id_gets_no_cache(self):
        for name in ("schedule", "workflow_dispatch"):
            event = {"ref": "refs/heads/main", "repository": {"full_name": REPOSITORY}}
            self.assertIsNone(namespace_for(event, name, REPOSITORY))
            self.assertEqual(
                namespace_for(_push("refs/heads/main"), name, REPOSITORY), DEFAULT_BRANCH_NAMESPACE
            )

    def test_a_stray_file_beside_the_cache_directories_does_not_stop_admission_or_pruning(self):
        old = self.cache.acquire(DEFAULT_BRANCH_NAMESPACE, IMAGE_A, 77)
        self.cache.release(old)
        for folder in (self.state / "package-cache" / DEFAULT_BRANCH_NAMESPACE,):
            (folder / ".DS_Store").write_text("noise")
            (folder / "notes.txt").write_text("noise")
            (folder / "UPPERCASE").mkdir()
        fresh = self.cache.acquire(DEFAULT_BRANCH_NAMESPACE, IMAGE_B, 77)
        self.cache.release(fresh)
        self.now += 100
        self.assertEqual(sorted(self.cache.prune()), sorted([old.root, fresh.root]))
        folder = self.state / "package-cache" / DEFAULT_BRANCH_NAMESPACE
        self.assertTrue((folder / ".DS_Store").exists())
        self.assertIsNone(self.cache.reclaim())

    def test_a_file_with_several_hard_links_is_charged_once(self):
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        (mount.registry / "a").write_bytes(b"x" * 1_000_000)
        os.link(mount.registry / "a", mount.registry / "b")
        os.link(mount.registry / "a", mount.git / "c")
        self.cache.release(mount)
        kept = PackageCache(self.state, max_age_seconds=100, max_bytes=1_500_000)
        again = kept.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertTrue((again.registry / "a").exists())
        self.assertEqual((again.registry / "a").stat().st_nlink, 3)
        kept.release(again)
        two = PackageCache(self.state, max_age_seconds=100, max_bytes=1_000_000)
        emptied = two.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.assertFalse((emptied.registry / "a").exists())

    def _open_descriptors(self):
        return len(os.listdir("/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"))

    def test_a_failed_listing_closes_every_descriptor_in_prune_and_reclaim(self):
        mount = self.cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
        self.cache.release(mount)
        self.now += 100
        real = os.listdir
        for call in (self.cache.prune, self.cache.reclaim):
            before = self._open_descriptors()

            def broken(target, real=real):
                if isinstance(target, int):
                    raise OSError("listing failed")
                return real(target)

            with (
                patch("execution_core.package_cache.os.listdir", broken),
                self.assertRaises(OSError),
            ):
                call()
            self.assertEqual(self._open_descriptors(), before, call.__name__)
        self.assertEqual(self.cache.prune(), [mount.root])
        self.assertEqual(self._open_descriptors(), before)

    def test_a_name_that_is_not_a_namespace_and_an_image_key_is_refused(self):
        for namespace, image in (("../x", IMAGE_A), (BRANCH_NAMESPACE, "../../etc"), ("x", "y")):
            with self.assertRaises(ValueError):
                self.cache.acquire(namespace, image, 77)

    def test_the_limits_must_be_positive_integers(self):
        for bad in (0, -1, True, 1.5, "7"):
            with self.assertRaises(ValueError):
                PackageCache(self.state, max_age_seconds=bad)
            with self.assertRaises(ValueError):
                PackageCache(self.state, max_bytes=bad)


def _clone(directory, url="https://github.com/owner/project.git"):
    clone = Path(directory) / "clone"
    clone.mkdir()
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    if url is not None:
        subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", url], check=True)
    return clone


class WorkerBudgetTests(unittest.TestCase):
    def test_the_cache_counts_toward_the_budget_and_is_freed_before_a_run_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state")
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                mount = worker.package_cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
                (mount.registry / "crate").write_bytes(b"x" * 100_000)
                worker.package_cache.release(mount)
                worker.state_usage.sync_path(worker.package_cache.root)
                self.assertEqual(worker.state_usage.total, usage(worker.state))
                self.assertGreaterEqual(worker.state_usage.total, 100_000)
                with worker.guard:
                    worker.disk_budget = worker.state_usage.total - 50_000
                    self.assertFalse(worker._over_budget(0))
                self.assertFalse(mount.root.exists())
                self.assertEqual(worker.state_usage.total, usage(worker.state))
            finally:
                worker.close()

    def test_a_cache_in_use_is_not_freed_to_make_room(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state")
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                mount = worker.package_cache.acquire(BRANCH_NAMESPACE, IMAGE_A, 77)
                (mount.registry / "crate").write_bytes(b"x" * 100_000)
                worker.state_usage.sync_path(worker.package_cache.root)
                with worker.guard:
                    worker.disk_budget = worker.state_usage.total - 50_000
                    self.assertTrue(worker._over_budget(0))
                self.assertTrue((mount.registry / "crate").exists())
            finally:
                worker.close()

    def test_a_run_gets_the_cache_of_its_event_and_image_and_an_override_or_fork_gets_none(self):
        digest = "sha256:" + IMAGE_A + "0" * 48
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(_clone(directory), Path(directory) / "state")
            self.assertEqual(worker.cache_repository, REPOSITORY)
            main = _push("refs/heads/main")
            mount = worker._acquire_package_cache(main, "push", {}, digest)
            self.assertEqual(mount.namespace, DEFAULT_BRANCH_NAMESPACE)
            self.assertEqual(
                mount.root, worker.state / "package-cache" / DEFAULT_BRANCH_NAMESPACE / IMAGE_A
            )
            pull = worker._acquire_package_cache(_pull(), "pull_request", {}, digest)
            self.assertEqual(pull.namespace, BRANCH_NAMESPACE)
            self.assertIsNone(
                worker._acquire_package_cache(main, "push", {"untrusted_override": True}, digest)
            )
            self.assertIsNone(
                worker._acquire_package_cache(_pull(head_id=900), "pull_request", {}, digest)
            )
            self.assertIsNone(worker._acquire_package_cache(main, "push", {}, "not a digest"))

    def test_a_run_on_a_ref_listed_for_secrets_gets_no_cache_but_an_ordinary_branch_does(self):
        digest = "sha256:" + IMAGE_A + "0" * 48
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(_clone(directory), Path(directory) / "state")
            worker.secret_refs = ["refs/heads/release/1"]
            self.assertIsNone(
                worker._acquire_package_cache(_push("refs/heads/release/1"), "push", {}, digest)
            )
            ordinary = worker._acquire_package_cache(_push("refs/heads/topic"), "push", {}, digest)
            self.assertEqual(ordinary.namespace, BRANCH_NAMESPACE)

    def test_a_worker_that_cannot_verify_its_repository_gets_no_cache(self):
        digest = "sha256:" + IMAGE_A + "0" * 48
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(_clone(directory, url=None), Path(directory) / "state")
            self.assertIsNone(worker.cache_repository)
            self.assertIsNone(
                worker._acquire_package_cache(_push("refs/heads/main"), "push", {}, digest)
            )
            self.assertFalse((worker.state / "package-cache").exists())

    def test_an_event_for_another_repository_gets_no_cache_on_a_verified_worker(self):
        digest = "sha256:" + IMAGE_A + "0" * 48
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(
                _clone(directory, "https://github.com/owner/other.git"), Path(directory) / "state"
            )
            self.assertIsNone(
                worker._acquire_package_cache(_push("refs/heads/main"), "push", {}, digest)
            )

    def test_a_run_gets_no_cache_when_it_is_off_or_cannot_be_prepared(self):
        digest = "sha256:" + IMAGE_A + "0" * 48
        with tempfile.TemporaryDirectory() as directory:
            off = Worker(_clone(directory), Path(directory) / "off", package_cache=False)
            self.assertIsNone(
                off._acquire_package_cache(_push("refs/heads/main"), "push", {}, digest)
            )
            blocked = Worker(directory + "/clone", Path(directory) / "blocked")
            blocked.state.mkdir()
            (blocked.state / "package-cache").write_text("a file, not a directory")
            self.assertIsNone(
                blocked._acquire_package_cache(_push("refs/heads/main"), "push", {}, digest)
            )

    def test_the_cache_can_be_turned_off(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state", package_cache=False)
            self.assertIsNone(worker.package_cache)
            with self.assertRaises(ValueError):
                Worker(directory, Path(directory) / "state", package_cache="yes")


_IMAGE_WITH_CARGO = (
    "FROM python:3.12-slim\nENV CARGO_HOME=/opt/cargo\nRUN mkdir -m 0777 /opt/cargo\n"
)
_IMAGE_WITHOUT_CARGO = "FROM python:3.12-slim\n"
_WRITE = """\
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: |
          printf '%s' downloaded > "$CARGO_HOME/registry/crate-1.0.crate"
          printf '%s' checkout > "$CARGO_HOME/git/dep"
          printf '%s' wheel > "$PIP_CACHE_DIR/wheel"
          printf '%s' tarball > "$npm_config_cache/tarball"
          printf '%s' module > "$GOMODCACHE/module"
          mkdir -p "$CARGO_INSTALL_ROOT/bin" "$PIPX_BIN_DIR"
          printf '#!/bin/sh\\nprintf installed-tool\\n' > "$CARGO_INSTALL_ROOT/bin/cachedtool"
          printf '#!/bin/sh\\nprintf pipx-tool\\n' > "$PIPX_BIN_DIR/pipxtool"
          chmod +x "$CARGO_INSTALL_ROOT/bin/cachedtool" "$PIPX_BIN_DIR/pipxtool"
"""
_READ = """\
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {
            printf 'registry=%s\\n' "$(cat "$CARGO_HOME/registry/crate-1.0.crate" 2>/dev/null)"
            printf 'git=%s\\n' "$(cat "$CARGO_HOME/git/dep" 2>/dev/null)"
            printf 'pip=%s\\n' "$(cat "$PIP_CACHE_DIR/wheel" 2>/dev/null)"
            printf 'npm=%s\\n' "$(cat "$npm_config_cache/tarball" 2>/dev/null)"
            printf 'go=%s\\n' "$(cat "$GOMODCACHE/module" 2>/dev/null)"
            printf 'tool=%s\\n' "$(cachedtool 2>/dev/null)"
            printf 'pipx=%s\\n' "$(pipxtool 2>/dev/null)"
            printf 'root=%s\\n' "$CARGO_INSTALL_ROOT"
            printf 'pipdir=%s\\n' "$(python3 -m pip cache dir 2>/dev/null)"
          } > "$GITHUB_WORKSPACE/seen.txt"
"""
_OVERRIDE = """\
on: push
env:
  PIP_CACHE_DIR: /tmp/own-pip
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: printf '%s' "$PIP_CACHE_DIR" > "$GITHUB_WORKSPACE/seen.txt"
"""
_LOCK_HOLD = """\
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: |
          python3 - <<'PY'
          import fcntl, os, time
          handle = open(os.environ["CARGO_HOME"] + "/.package-cache", "a")
          fcntl.flock(handle, fcntl.LOCK_EX)
          open(os.environ["GITHUB_WORKSPACE"] + "/held", "w").write("1")
          time.sleep(8)
          PY
"""
_LOCK_TRY = """\
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: |
          python3 - <<'PY'
          import fcntl, os
          handle = open(os.environ["CARGO_HOME"] + "/.package-cache", "a")
          try:
              fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
              answer = "free"
          except BlockingIOError:
              answer = "blocked"
          open(os.environ["GITHUB_WORKSPACE"] + "/answer", "w").write(answer)
          PY
"""
_NOTHING = [
    "registry=",
    "git=",
    "pip=",
    "npm=",
    "go=",
    "tool=",
    "pipx=",
    "root=",
]
_SEEN = [
    "registry=downloaded",
    "git=checkout",
    "pip=wheel",
    "npm=tarball",
    "go=module",
    "tool=installed-tool",
    "pipx=pipx-tool",
    f"root={CARGO_TOOLS_ROOT}",
    f"pipdir={CACHE_ROOT}/pip",
]


class JobCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise AssertionError("Docker is required")
        cls.image_root = tempfile.TemporaryDirectory(prefix="cache-image-")
        cls.images = {}
        cls.tags = []
        suite = uuid.uuid4().hex[:12]
        for name, text in (("with", _IMAGE_WITH_CARGO), ("without", _IMAGE_WITHOUT_CARGO)):
            folder = Path(cls.image_root.name) / name
            folder.mkdir()
            (folder / "Dockerfile").write_text(text)
            tag = f"rookrunner-cache-{suite}-{name}"
            subprocess.run(
                ["docker", "build", "--quiet", "-t", tag, folder], check=True, capture_output=True
            )
            cls.tags.append(tag)
            cls.images[name] = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

    @classmethod
    def tearDownClass(cls):
        for tag in cls.tags:
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
        cls.image_root.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cache-job-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = PackageCache(self.root / "cache-state")
        self.count = 0
        # A wrapper around the real client records every create, so teardown
        # removes exactly the containers this test made and no one else's.
        self.log = self.root / "docker-calls"
        self.docker = self.root / "docker"
        self.docker.write_text(
            "#!/usr/bin/env python3\n"
            "import subprocess, sys\n"
            f"with open({str(self.log)!r}, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(repr(sys.argv[1:]) + '\\n')\n"
            f"raise SystemExit(subprocess.call([{shutil.which('docker')!r}, *sys.argv[1:]]))\n"
        )
        self.docker.chmod(0o755)

    def tearDown(self):
        if not self.log.exists():
            return
        for line in self.log.read_text().splitlines():
            call = ast.literal_eval(line)
            if call[:1] == ["create"] and "--name" in call:
                name = call[call.index("--name") + 1]
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    def _job(self, workflow, image="with", mount=None):
        self.count += 1
        root = self.root / f"job-{self.count}"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(workflow),
            self.images[image],
            EVENT,
            docker=str(self.docker),
            package_cache=mount,
        )
        return result, workspace

    def _mount(self, namespace=BRANCH_NAMESPACE):
        return self.cache.acquire(namespace, IMAGE_A, 77)

    def _seen(self, workspace):
        return (workspace / "seen.txt").read_text().splitlines()

    def test_a_later_job_sees_what_an_earlier_job_left_for_every_package_manager(self):
        mount = self._mount()
        wrote, _workspace = self._job(_WRITE, mount=mount)
        self.assertEqual(wrote["status"], "succeeded", wrote)
        read, workspace = self._job(_READ, mount=mount)
        self.assertEqual(read["status"], "succeeded", read)
        self.assertEqual(self._seen(workspace), _SEEN)

    def test_a_job_without_the_cache_sees_none_of_it(self):
        wrote, _workspace = self._job(_WRITE, mount=self._mount())
        self.assertEqual(wrote["status"], "succeeded", wrote)
        read, workspace = self._job(_READ, mount=None)
        self.assertEqual(read["status"], "succeeded", read)
        seen = self._seen(workspace)
        self.assertEqual(seen[: len(_NOTHING)], _NOTHING)
        self.assertNotIn(CACHE_ROOT, seen[-1])

    def test_another_namespace_does_not_see_the_files(self):
        wrote, _workspace = self._job(_WRITE, mount=self._mount(BRANCH_NAMESPACE))
        self.assertEqual(wrote["status"], "succeeded", wrote)
        read, workspace = self._job(_READ, mount=self._mount(DEFAULT_BRANCH_NAMESPACE))
        self.assertEqual(read["status"], "succeeded", read)
        self.assertEqual(self._seen(workspace)[: len(_NOTHING) - 1], _NOTHING[:-1])

    def test_an_image_without_a_cargo_home_still_gets_the_other_caches_but_not_cargos(self):
        wrote, _workspace = self._job(
            _WRITE.replace('"$CARGO_HOME/registry/crate-1.0.crate"', '"/dev/null"').replace(
                '"$CARGO_HOME/git/dep"', '"/dev/null"'
            ),
            image="without",
            mount=self._mount(),
        )
        self.assertEqual(wrote["status"], "succeeded", wrote)
        read, workspace = self._job(_READ, image="without", mount=self._mount())
        self.assertEqual(read["status"], "succeeded", read)
        seen = self._seen(workspace)
        self.assertEqual(seen[:2], ["registry=", "git="])
        self.assertEqual(
            seen[2:7],
            ["pip=wheel", "npm=tarball", "go=module", "tool=installed-tool", "pipx=pipx-tool"],
        )

    def test_a_workflow_can_replace_a_default_cache_location(self):
        mount = self._mount()
        result, workspace = self._job(_OVERRIDE, mount=mount)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual((workspace / "seen.txt").read_text(), "/tmp/own-pip")

    def test_two_jobs_at_once_share_cargos_package_cache_lock(self):
        mount = self._mount()
        holder = {}

        def hold():
            holder["result"], holder["workspace"] = self._job(_LOCK_HOLD, mount=mount)

        started = self.root / "job-1"
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            deadline = time.monotonic() + 60
            while not list(started.glob("**/held")):
                self.assertLess(time.monotonic(), deadline, "the first job never took the lock")
                time.sleep(0.2)
            tried, workspace = self._job(_LOCK_TRY, mount=mount)
        finally:
            thread.join()
        self.assertEqual(tried["status"], "succeeded", tried)
        self.assertEqual((workspace / "answer").read_text(), "blocked")
        self.assertEqual(holder["result"]["status"], "succeeded", holder["result"])

    def test_the_cache_files_stay_owned_by_the_worker_user(self):
        mount = self._mount()
        self._job(_WRITE, mount=mount)
        for path in (
            mount.registry / "crate-1.0.crate",
            mount.git / "dep",
            mount.shared / "pip" / "wheel",
            mount.shared / "cargo-tools" / "bin" / "cachedtool",
        ):
            self.assertEqual(path.stat().st_uid, os.getuid())


class CreateArgsTests(unittest.TestCase):
    def test_the_cache_mounts_come_before_the_image(self):
        args = _create_args(
            "job",
            "/workspace",
            "/private",
            "/commands",
            "image@sha256:aa",
            "bridge",
            None,
            None,
            {"home": "/home", "runner-temp": "/temp", "tool-cache": "/tools"},
            None,
            None,
            cache_args=CacheMount(
                "branches", IMAGE_A, Path("/state/package-cache/branches") / IMAGE_A
            ).mount_args("/usr/local/cargo"),
        )
        image = args.index("image@sha256:aa")
        wanted = (
            f"type=bind,source=/state/package-cache/branches/{IMAGE_A}/registry,"
            "destination=/usr/local/cargo/registry"
        )
        self.assertIn(wanted, args)
        self.assertLess(args.index(wanted), image)

    def test_no_cache_adds_no_mount(self):
        args = _create_args(
            "job",
            "/workspace",
            "/private",
            "/commands",
            "image@sha256:aa",
            "bridge",
            None,
            None,
            {"home": "/home", "runner-temp": "/temp", "tool-cache": "/tools"},
            None,
            None,
        )
        self.assertFalse([part for part in args if "package-cache" in part])


if __name__ == "__main__":
    unittest.main()
