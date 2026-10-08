"""Single-user Unix worker.

Version 0 submits a synthetic development fixture. It never launches commands
or reads a workflow. Version 1 stages the repository, plans one selected job, copies ancestor
history when that plan has fetch-depth 0, publishes and verifies the
snapshot, and commits a queued run. The scheduler
then materializes an attempt, records it, and runs that plan in one
caller-pinned container. That container uses Docker network `bridge` unless
this worker was started with network `none`. It mounts the Docker engine
socket only when this worker was started with the docker socket flag. The
job then keeps the caller uid and is added to the groups that can open
that socket. `worker --node24 DIR` mounts that directory read-only at
`/opt/node24`. It is not added to `PATH`. The worker does not download Node.
`worker --runner-image DIGEST` is the image for a submit or poll that
omits `image` when every selected job has `runs-on: ubuntu-latest`.
The worker does not pull, build, or publish it. An explicit `image`
still wins.
`worker --secrets` takes no path and requires `--github-repository`.
`worker --app-key` accepts only the Rookrunner App key path. The worker
opens that file only when a job mints a Contents-read token.
`--secret-ref` and `--secret-pusher` may be repeated.
`--docker-socket` combined with `--app-key` or `--secrets` refuses
unless `--runner-image` is set and the `~/Secrets` probe exits 0.
A worker that starts with `--docker-socket` warns that the exposure
includes the key directory and the secret root. When `--secrets` is
set and the allowlist matches, a step `env` or `with` expression that
is exactly `secrets.NAME` receives that file's value on that step.
An exact `secrets.NAME` in `run` is rewritten to `${RR_SECRET_NAME}`
for bash and sh. When `--app-key` is set, the resolved permissions
allow Contents read, and a step needs a token, the worker mints one
installation token for that job. The mint names the repository and
Contents read. The token is revoked when the job ends. The record
stores the attempt, not the token. `permissions: {}` mints nothing.
A remote `node24` main is copied into the attempt and mounted read-write
at `/actions`. The content store is not mounted. `post` runs after that
job's main steps when the main ran. `pre` stays rejected.
A concurrency group is enforced on this one worker when the run is
accepted. `queue: single` replaces another queued run in that group.
`cancel-in-progress` also cancels the running run. `queue: max` keeps
at most 100 pending runs in the group. Those running ids are recorded
only after the action-store commit returns. Cancelling a running workflow
stops that container
before the run is recorded cancelled. If the container is still present, the
run is lost and a new workflow attempt is refused until this process stops.
Restart records the container beside the attempt, removes only that
container, and does not launch the interrupted attempt again. A submission
that would exceed the configured disk budget is refused. Active runs and
their evidence stay. A finished workflow attempt publishes a manifest of
the regular files it wrote under its workspace. Selected upload files are
named there, including unchanged files. A missing selected file fails a
job that would otherwise succeed. Development fixtures do not.
"""

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
import uuid
from datetime import datetime, UTC

from . import __version__
from .actions import (
    DEFAULT_ACTION_REMOTE,
    ActionStorageFull,
    ActionStore,
    ActionUnavailable,
    stage_node24_actions,
)
from .artifacts import ArtifactError, file_identity, named_manifest, read_bytes
from .upload import load_uploads
from .attempt import AttemptError, materialize_attempt
from .concurrency import decide, eligible, resolve_groups
from .disk import (
    DEFAULT_DISK_BUDGET,
    FINISHED_RUN_FOLDER_COUNT,
    FINISHED_RUN_FOLDER_SECONDS,
    usage,
)
from .expr import ExprError
from .node24 import MOUNT as _NODE24_MOUNT
from .node24 import inspect_node24
from .plan import (
    PlanError,
    plan_needs_history,
    plan_snapshot,
    remote_action_records,
    snapshot_workflow_bytes,
    workflow_on,
)
from .trigger import (
    MAX_CHANGED_FILES,
    MAX_PATH_LENGTH,
    submission_triggered,
)
from .run import (
    CONTAINER_NAME,
    DEFAULT_NETWORK,
    ContainerLease,
    RunError,
    owned_container_present,
    release_owned_container,
    run_job,
)
from .protocol import (
    MAX_CURSOR,
    MAX_JSON_DEPTH,
    MAX_LIST_PAGE,
    MAX_LOG_PAGE,
    MAX_MESSAGE,
    MAX_OFFSET,
    MAX_QUEUE,
    METHODS,
    TERMINAL,
    Fault,
    canonical,
    error_response,
    fields,
    integer,
    invalid,
    is_integer,
    strict_json,
    utf8_string,
    valid_id,
)
from .snapshot import CaptureError, SourceCapture
from .checks import check_mapping, check_summary
from .job_token import JobTokenConfig, public_token_revocation
from .secrets import SecretAccess
from .socket_lock import HOST_CONTROL_WARNING, accept_socket_flags
from .status import DEFAULT_API_BASE, MAX_CONTEXT_LENGTH, TERMINAL_STATUS, github_state
from .verify import VerifyError, verify_snapshot

_STORAGE_FULL = "worker storage is full; free space before retrying"
_ATTEMPT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_STATUS_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
_STEP_TEXT = 65536
_QUEUED_ROWS = (
    "SELECT record, request FROM runs "
    "WHERE json_extract(record, '$.state')='queued' "
    "ORDER BY sequence"
)
# While this process holds an unresolved container, leave queued workflow jobs
# queued. Development fixtures own no container and can still run. Restart
# keeps a separate retained set and blocks only reuse of those identities.
_QUEUED_FIXTURE_ROWS = (
    "SELECT record, request FROM runs "
    "WHERE json_extract(record, '$.state')='queued' "
    "AND json_extract(record, '$.input.kind')='development_fixture' "
    "ORDER BY sequence"
)
_CONCURRENCY_NOTE = (
    "Concurrency is enforced on this one worker. A group name is case "
    "insensitive. queue max keeps at most 100 pending runs in that group."
)


def now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _finished_stamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp


def cursor(kind, identity, offset):
    return base64.urlsafe_b64encode(canonical([kind, identity, offset]).encode()).decode()


def offset_from(value, kind, identity):
    if not utf8_string(value, 1, MAX_CURSOR):
        invalid("cursor must be a nonempty UTF-8 string of at most 1024 characters")
    try:
        decoded = strict_json(base64.b64decode(value, altchars=b"-_", validate=True))
        if (
            not isinstance(decoded, list)
            or len(decoded) != 3
            or decoded[:2] != [kind, identity]
            or type(decoded[2]) is not int
            or not 0 <= decoded[2] <= MAX_OFFSET
        ):
            raise ValueError()
        return decoded[2]
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Fault("CURSOR_EXPIRED", "invalid cursor or cursor belongs to another query") from None


class _Abandoned(Exception):
    pass


def conflicts_with_unresolved(unresolved, attempt_id=None, container_name=None):
    """Return whether this process refuses a new workflow attempt.

    Any recorded unresolved attempt blocks a new workflow attempt. The same
    attempt id or container name is also a conflict. The map is in memory and
    is not reloaded after restart.
    """

    if attempt_id is not None and attempt_id in unresolved:
        return True
    if container_name is not None and container_name in unresolved.values():
        return True
    return bool(unresolved)


def reuses_unresolved_identity(owners, attempt_id=None, container_name=None):
    """Return whether this attempt would reuse an unresolved container or workspace.

    A different attempt id and container name is not a conflict. An empty map
    is not a conflict.
    """

    if not owners:
        return False
    if attempt_id is not None and attempt_id in owners:
        return True
    return container_name is not None and container_name in owners.values()


def _error_text(exc):
    if isinstance(exc, OSError):
        return "workflow attempt could not be prepared"
    text = "".join(ch for ch in " ".join(str(exc).split()) if not 0xD800 <= ord(ch) <= 0xDFFF)
    return text[:512] or "workflow attempt could not be prepared"


def _public_steps(steps):
    stored = []
    for step in steps:
        item = {
            "index": step["index"],
            "id": step.get("id"),
            "name": step.get("name"),
            "status": step["status"],
            "exit_code": step["exit_code"],
            "stdout": step.get("stdout") or "",
            "stderr": step.get("stderr") or "",
            "error": step.get("error"),
        }
        job_id = step.get("job_id")
        if isinstance(job_id, str) and job_id:
            item["job_id"] = job_id
        for key in ("id", "name"):
            if item[key] == "":
                item[key] = None
        for key in ("stdout", "stderr"):
            if len(item[key]) > _STEP_TEXT:
                item[key] = item[key][:_STEP_TEXT]
        if isinstance(item["error"], str):
            text = "".join(ch for ch in item["error"] if not 0xD800 <= ord(ch) <= 0xDFFF)[:512]
            item["error"] = text or None
        elif item["error"] is not None:
            item["error"] = None
        stored.append(item)
    return stored


def _workflow_log(steps):
    """UTF-8 bytes of each executed step's stdout, then its stderr.

    `run.logs` pages this blob with the existing protocol page size. The
    captured output is not cut to a separate log-size cap.
    """

    chunks = []
    for step in steps:
        for key in ("stdout", "stderr"):
            text = step.get(key) or ""
            if isinstance(text, str) and text:
                chunks.append(text.encode("utf-8"))
    return b"".join(chunks)


def _step_failure_message(outcome, image_ok):
    if not image_ok:
        return "image digest does not match the accepted pin"
    for step in outcome["steps"]:
        if step.get("status") == "failed" and step.get("error"):
            label = step.get("name") or step.get("id") or f"step {step.get('index')}"
            return f"{label}: {step['error']}"
    return "step failed without an exit code"


class Worker:
    def __init__(
        self,
        repository,
        state,
        disk_budget=None,
        *,
        network=DEFAULT_NETWORK,
        docker_socket=False,
        action_remote=None,
        node24=None,
        runner_image=None,
        secrets=False,
        github_repository=None,
        app_key=None,
        secret_refs=None,
        secret_pushers=None,
        api_base=None,
    ):
        if network not in {DEFAULT_NETWORK, "none"}:
            raise ValueError("container network must be bridge or none")
        if type(docker_socket) is not bool:
            raise ValueError("docker socket must be a boolean")
        if action_remote is None:
            action_remote = DEFAULT_ACTION_REMOTE
        if action_remote != DEFAULT_ACTION_REMOTE:
            remote_root = Path(action_remote)
            if not remote_root.is_absolute() or not remote_root.is_dir():
                raise ValueError(
                    "action remote must be https://github.com or an absolute directory"
                )
            action_remote = str(remote_root)
        self.network = network
        self.docker_socket = docker_socket
        self.node24 = None if node24 is None else inspect_node24(node24)
        self.runner_image = _accept_runner_image(runner_image)
        self.runner_image_digest = (
            None
            if self.runner_image is None
            else "sha256:" + self.runner_image.rsplit("sha256:", 1)[1]
        )
        flags = accept_socket_flags(
            docker_socket=docker_socket,
            secrets=secrets,
            github_repository=github_repository,
            app_key=app_key,
            secret_refs=secret_refs,
            secret_pushers=secret_pushers,
            runner_image=self.runner_image,
        )
        self.secrets = flags.secrets
        self.github_repository = flags.github_repository
        self.app_key = flags.app_key
        self.api_base = DEFAULT_API_BASE if api_base is None else api_base
        self.secret_refs = flags.secret_refs
        self.secret_pushers = flags.secret_pushers
        self.host_control_warning = HOST_CONTROL_WARNING if docker_socket else None
        self.action_remote = action_remote
        self._action_store = None
        if disk_budget is None:
            disk_budget = DEFAULT_DISK_BUDGET
        # bool is an int subclass. A flag is not a byte count.
        if type(disk_budget) is not int or disk_budget < 0:
            raise ValueError("disk budget must be a non-negative integer")
        self.disk_budget = disk_budget
        self.repository = str(Path(repository).resolve(strict=True))
        if not Path(self.repository).is_dir():
            raise ValueError("repository must be a directory")
        self.state = Path(state).absolute()
        self.stop = threading.Event()
        self.guard = threading.RLock()
        self.db = None
        self.lock = None
        self.server = None
        self.scheduler = None
        self.execution_error = None
        self.live = {}
        self.unresolved = {}
        self.retained = {}
        self._pending_concurrency_cancels = []
        self.socket_path = self.state / "worker.sock"

    def start(self):
        # Never change permissions on a pre-existing shared or foreign directory.
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("state directory must be owned by you with mode 0700, not a symlink")
        self.lock = os.open(
            self.state / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(self.state / "runs.sqlite3", check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    submission_key TEXT NOT NULL UNIQUE,
                    request TEXT NOT NULL,
                    record TEXT NOT NULL,
                    log BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    name TEXT,
                    UNIQUE(run_id, path)
                );
                CREATE TABLE IF NOT EXISTS status_posts (
                    run_id TEXT NOT NULL,
                    context TEXT NOT NULL,
                    sha TEXT NOT NULL,
                    state TEXT NOT NULL,
                    PRIMARY KEY (run_id, context, sha, state)
                );
                CREATE TABLE IF NOT EXISTS check_posts (
                    run_id TEXT NOT NULL,
                    context TEXT NOT NULL,
                    sha TEXT NOT NULL,
                    check_run_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    conclusion TEXT,
                    PRIMARY KEY (run_id, context, sha)
                );
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(artifacts)")}
            if "name" not in columns:
                self.db.execute("ALTER TABLE artifacts ADD COLUMN name TEXT")
            with self.db:
                root = self.db.execute(
                    "SELECT value FROM metadata WHERE key='repository'"
                ).fetchone()
                if root and root[0] != self.repository:
                    raise ValueError(
                        "ROOT_MISMATCH: state directory is bound to another repository"
                    )
                self.db.execute(
                    "INSERT OR IGNORE INTO metadata VALUES ('repository', ?)", (self.repository,)
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO metadata VALUES ('worker_id', ?)", (str(uuid.uuid4()),)
                )
            self.worker_id = self.db.execute(
                "SELECT value FROM metadata WHERE key='worker_id'"
            ).fetchone()[0]
            self._reconcile_interrupted()
            if os.path.lexists(self.socket_path):
                if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                    raise ValueError("refusing to replace a non-socket worker.sock")
                self.socket_path.unlink()
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server.bind(str(self.socket_path))
            self.owns_socket = True
            os.chmod(self.socket_path, 0o600)
            self.server.listen(16)
            self.server.settimeout(0.2)
            self.scheduler = threading.Thread(target=self.schedule, daemon=True)
            self.scheduler.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stop.set()
        if self.scheduler:
            self.scheduler.join()
            self.scheduler = None
        if self.server:
            self.server.close()
            self.server = None
        if getattr(self, "owns_socket", False):
            self.socket_path.unlink(missing_ok=True)
            self.owns_socket = False
        if self.db:
            self.db.close()
            self.db = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def _records(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT record FROM runs").fetchall()]

    def _reconcile_interrupted(self):
        """Mark interrupted attempts lost before accepting work that could overlap them.

        A development fixture has no container. A workflow attempt records its
        container name beside the attempt, not on the run. Restart removes only
        that container. The public record stays free of the name and of host paths.
        """

        with self.db:
            running = [record for record in self._records() if record["state"] == "running"]
        updates = []
        for record in running:
            unresolved, name = self._release_running(record)
            updates.append((record, unresolved, name))
        with self.db:
            for record, unresolved, name in updates:
                self._store_restart_lost(record, unresolved)
                if unresolved:
                    identity = record.get("attempt_id") or record["run_id"]
                    self.retained[identity] = name
        self._load_retained()
        self._prune_finished_run_folders(0)

    def _release_running(self, record):
        kind = (record.get("input") or {}).get("kind")
        if kind != "workflow_job":
            return False, None
        status, name = self._ownership_status(record.get("attempt_id"))
        if status == "absent":
            return False, None
        if status != "name":
            return True, None
        try:
            gone = release_owned_container(name)
        except Exception:
            gone = False
        if gone:
            self._delete_ownership(record.get("attempt_id"))
            return False, None
        return True, name

    def _store_restart_lost(self, record, unresolved):
        self._publish_artifacts(record)
        record.update(
            state="lost",
            finished_at=now(),
            cancel_requested=False,
            error={
                "kind": "WORKER_INTERRUPTED",
                "message": (
                    "owned container cleanup was not confirmed"
                    if unresolved
                    else "worker stopped during execution"
                ),
            },
            cleanup="unresolved" if unresolved else "confirmed_no_external_resources",
        )
        self.save(record)
        if not unresolved:
            self._release_finished_attempt(record)

    def _load_retained(self):
        with self.db:
            rows = self._records()
        for record in rows:
            if record.get("state") != "lost" or record.get("cleanup") != "unresolved":
                continue
            attempt_id = record.get("attempt_id")
            if not attempt_id or attempt_id in self.retained:
                continue
            status, name = self._ownership_status(attempt_id)
            if status == "name" and owned_container_present(name):
                self.retained[attempt_id] = name
            elif status == "invalid":
                self.retained[attempt_id] = None
            elif status == "name":
                self._delete_ownership(attempt_id)

    def _ownership_directory(self):
        directory = self.state / "ownership"
        if not os.path.lexists(directory):
            return "absent", None
        if directory.is_symlink() or not directory.is_dir():
            return "invalid", None
        return "ok", directory

    def _ownership_status(self, attempt_id):
        if not _ATTEMPT_ID.fullmatch(attempt_id or ""):
            return "absent", None
        state, directory = self._ownership_directory()
        if state != "ok":
            return state, None
        path = directory / attempt_id
        if not os.path.lexists(path):
            return "absent", None
        try:
            info = path.lstat()
        except OSError:
            return "invalid", None
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64:
            return "invalid", None
        try:
            text = path.read_text(encoding="ascii")
        except (OSError, UnicodeError):
            return "invalid", None
        name = text.strip()
        if not CONTAINER_NAME.fullmatch(name):
            return "invalid", None
        return "name", name

    def _write_ownership(self, attempt_id, name):
        if not _ATTEMPT_ID.fullmatch(attempt_id or "") or not CONTAINER_NAME.fullmatch(name):
            raise OSError("attempt identity is not accepted")
        state, directory = self._ownership_directory()
        if state == "invalid":
            raise OSError("attempt identity is not accepted")
        if state == "absent":
            directory = self.state / "ownership"
            directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        if directory.is_symlink() or not directory.is_dir():
            raise OSError("attempt identity is not accepted")
        target = directory / attempt_id
        if os.path.lexists(target) and target.is_symlink():
            raise OSError("attempt identity is not accepted")
        fd, temporary = tempfile.mkstemp(prefix=".", dir=directory)
        try:
            os.write(fd, name.encode("ascii") + b"\n")
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        except Exception:
            os.close(fd)
            os.unlink(temporary)
            raise
        os.close(fd)
        try:
            os.replace(temporary, target)
        except Exception:
            os.unlink(temporary)
            raise
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        os.chmod(target, 0o600)

    def _delete_ownership(self, attempt_id):
        state, directory = self._ownership_directory()
        if state != "ok" or not _ATTEMPT_ID.fullmatch(attempt_id or ""):
            return
        path = directory / attempt_id
        if path.is_symlink() or not path.is_file():
            return
        try:
            path.unlink()
        except OSError:
            return

    def _blocked_attempt(self, attempt_id, container_name=None):
        return reuses_unresolved_identity(
            self.retained, attempt_id, container_name
        ) or reuses_unresolved_identity(self.unresolved, attempt_id, container_name)

    def _blocked_container_names(self):
        names = []
        for owners in (self.retained, self.unresolved):
            for value in owners.values():
                if isinstance(value, str) and CONTAINER_NAME.fullmatch(value):
                    names.append(value)
        return frozenset(names)

    def _refuse_reused_attempt(self, record):
        with self.guard, self.db:
            current = self.get(record["run_id"])
            if current["state"] in TERMINAL:
                self.live.pop(current["run_id"], None)
                return current
            current.update(
                state="lost",
                cancel_requested=False,
                exit_code=None,
                finished_at=now(),
                error={
                    "kind": "WORKER_INTERRUPTED",
                    "message": "an unresolved attempt still owns this container or workspace",
                },
                cleanup="unresolved",
            )
            self.save(current)
            self.live.pop(current["run_id"], None)
            identity = current.get("attempt_id") or current["run_id"]
            if identity not in self.retained:
                self.retained[identity] = self.unresolved.get(identity)
            return current

    def save(self, record):
        self.db.execute(
            "UPDATE runs SET record=? WHERE id=?", (canonical(record), record["run_id"])
        )

    def get(self, run_id):
        if not utf8_string(run_id, 1, 128):
            invalid("run_id must be a nonempty UTF-8 string of at most 128 characters")
        row = self.db.execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise Fault("RUN_NOT_FOUND", "run does not exist")
        return json.loads(row[0])

    def schedule(self):
        try:
            self.execute_queue()
        except Exception:
            # Fail closed if storage or execution breaks; never accept work into
            # a dead scheduler. Restart reconciles a still-running record as lost.
            with self.guard:
                self.execution_error = "scheduler stopped; restart worker to reconcile runs"

    def execute_queue(self):
        while not self.stop.is_set():
            lease = None
            picked = None
            with self.guard:
                picked = self._select_queued(conflicts_with_unresolved(self.unresolved))
                if picked:
                    record, request = picked
                    kind = record["input"]["kind"]
                    if kind in {"development_fixture", "workflow_job"}:
                        attempt_id = str(uuid.uuid4())
                        while self._blocked_attempt(attempt_id):
                            attempt_id = str(uuid.uuid4())
                        record.update(state="running", started_at=now(), attempt_id=attempt_id)
                        if kind == "workflow_job":
                            lease = ContainerLease()
                            lease.blocked_names = self._blocked_container_names()
                            lease.reserve = (
                                lambda name, attempt_id=attempt_id: self._write_ownership(
                                    attempt_id, name
                                )
                            )
                            self.live[record["run_id"]] = lease
                        with self.db:
                            self.save(record)
                    else:
                        self._fail_unclaimed(record)
                        picked = None
            if not picked:
                self.stop.wait(0.02)
                continue
            if kind == "development_fixture":
                self._finish_fixture(record, request)
            else:
                self._execute_workflow(record, request, lease)

    def _apply_concurrency(self, record):
        """Cancel queued peers in this transaction. Return running ids to stop."""

        peers = [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT record FROM runs WHERE json_extract(record, '$.state') IN ('queued', 'running')"
            )
        ]
        cancel_ids, reject = decide(peers, record)
        if reject:
            record.update(
                state="cancelled",
                cancel_requested=True,
                finished_at=now(),
                cleanup="confirmed_no_external_resources",
            )
            self.save(record)
            return []
        running_ids = []
        for peer in peers:
            if peer.get("run_id") not in cancel_ids:
                continue
            if peer.get("state") == "queued":
                self._mark_queued_cancelled(peer["run_id"])
            elif peer.get("state") == "running":
                running_ids.append(peer["run_id"])
        return running_ids

    def _mark_queued_cancelled(self, run_id):
        current = self.get(run_id)
        if current["state"] != "queued":
            return
        current.update(
            state="cancelled",
            cancel_requested=True,
            finished_at=now(),
            exit_code=None,
            error=None,
            cleanup="confirmed_no_external_resources",
        )
        self.save(current)
        self._release_finished_attempt(current)

    def _select_queued(self, fixture_only):
        """Return the next queued run that is not waiting on its own group."""

        sql = _QUEUED_FIXTURE_ROWS if fixture_only else _QUEUED_ROWS
        running = [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT record FROM runs WHERE json_extract(record, '$.state')='running'"
            )
        ]
        queued = []
        requests = []
        for row in self.db.execute(sql):
            queued.append(json.loads(row[0]))
            requests.append(json.loads(row[1]))
        chosen = eligible(queued, running)
        if chosen is None:
            return None
        index = next(
            position
            for position, record in enumerate(queued)
            if record["run_id"] == chosen["run_id"]
        )
        return chosen, requests[index]

    def _finish_fixture(self, record, request):
        interrupted = self.stop.wait(request["fixture"]["delay_ms"] / 1000)
        with self.guard, self.db:
            record = self.get(record["run_id"])
            if record["state"] in TERMINAL:
                return
            if interrupted:
                record.update(
                    state="lost",
                    error={
                        "kind": "WORKER_INTERRUPTED",
                        "message": "worker stopped during execution",
                    },
                )
            else:
                code = request["fixture"]["exit_code"]
                record.update(state="succeeded" if code == 0 else "failed", exit_code=code)
                self.db.execute(
                    "UPDATE runs SET log=? WHERE id=?",
                    (request["fixture"]["output"].encode(), record["run_id"]),
                )
            record.update(finished_at=now(), cleanup="confirmed_no_external_resources")
            self.save(record)
            self._release_finished_attempt(record)

    def _fail_unclaimed(self, record):
        record.update(
            state="failed",
            exit_code=None,
            started_at=now(),
            finished_at=now(),
            attempt_id=str(uuid.uuid4()),
            error={"kind": "SETUP_FAILED", "message": "queued run is not executable"},
            cleanup="confirmed_no_external_resources",
        )
        with self.db:
            self.save(record)
        self._release_finished_attempt(record)

    def _execute_workflow(self, record, request, lease):
        attempt_root = Path(self.state) / "attempts" / record["attempt_id"]
        try:
            if self._blocked_attempt(record.get("attempt_id")):
                self._refuse_reused_attempt(record)
                return
            try:
                outcome = self._run_accepted(record, request, lease)
            except _Abandoned:
                self._remove_workspace(attempt_root)
                return
            except Exception as exc:
                message = (
                    _error_text(exc)
                    if isinstance(
                        exc,
                        RunError
                        | AttemptError
                        | VerifyError
                        | PlanError
                        | OSError
                        | UnicodeError
                        | ValueError,
                    )
                    else "workflow attempt could not be prepared"
                )
                self._finish_setup_failure(record["run_id"], message)
                return
            self._finish_workflow(record["run_id"], outcome)
        finally:
            with self.guard:
                if self.get(record["run_id"])["state"] in TERMINAL:
                    self.live.pop(record["run_id"], None)

    def job_token_config(self):
        """Return mint settings when --app-key is set. This does not open the key."""

        if not isinstance(self.app_key, str) or self.app_key == "":
            return None
        return JobTokenConfig(
            self.app_key,
            self.api_base,
            state_dir=self.state,
            repository_root=self.repository,
            github_repository=self.github_repository,
        )

    def _run_accepted(self, record, request, owner=None):
        pinned = record["input"]
        event = request["event"]
        event_digest = hashlib.sha256(canonical(event).encode("ascii")).hexdigest()
        if event_digest != pinned["event_digest"]:
            raise RunError("SETUP_FAILED", "event digest does not match the accepted event")
        image = pinned["image_reference"]
        if not isinstance(image, str) or not (
            _IMAGE_ID.fullmatch(image) or _IMAGE_REF.fullmatch(image)
        ):
            raise RunError("SETUP_FAILED", "image is not pinned by digest")
        if "sha256:" + image.rsplit("sha256:", 1)[1] != pinned["image_digest"]:
            raise RunError("SETUP_FAILED", "image digest does not match the accepted pin")
        snapshot = Path(self.state) / "snapshots" / pinned["snapshot_id"]
        manifest = verify_snapshot(snapshot, pinned["digest"])
        if (
            manifest.get("workflow") != pinned["workflow"]
            or manifest.get("workflow_digest") != pinned["workflow_digest"]
        ):
            raise RunError("SETUP_FAILED", "snapshot workflow does not match the accepted digest")
        store = self.action_store()
        with store.guard:
            planned = plan_snapshot(snapshot, pinned["job_id"], action_store=store)
        if planned["digest"] != pinned["plan_digest"]:
            raise RunError("SETUP_FAILED", "planned digest does not match the accepted plan")
        if self._abandoned(record["run_id"]):
            raise _Abandoned()
        attempts = Path(self.state) / "attempts"
        if attempts.is_symlink():
            raise RunError("SETUP_FAILED", "attempt directory is not accepted")
        attempts.mkdir(mode=0o700, exist_ok=True)
        os.chmod(attempts, 0o700)
        try:
            reserve = usage(snapshot)
        except OSError as exc:
            raise RunError("SETUP_FAILED", _STORAGE_FULL) from exc
        # The snapshot is already in the state total. Reserve another copy
        # for the attempt workspace. Do not delete the snapshot or other evidence.
        if self._over_budget(reserve):
            raise RunError("SETUP_FAILED", _STORAGE_FULL)
        attempt_root = attempts / record["attempt_id"]
        workspace = attempt_root / "workspace"
        try:
            attempt_root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise RunError("SETUP_FAILED", "attempt directory is not accepted") from exc
        try:
            os.chmod(attempt_root, 0o700)
            materialize_attempt(snapshot, pinned["digest"], workspace)
        except Exception:
            self._remove_workspace(attempt_root)
            raise
        if self._abandoned(record["run_id"]):
            raise _Abandoned()
        try:
            actions_root = stage_node24_actions(
                planned["plan"],
                store,
                attempt_root / "actions",
                self._over_budget,
            )
        except ActionStorageFull as exc:
            raise RunError("SETUP_FAILED", _STORAGE_FULL) from exc
        except ActionUnavailable as exc:
            message = (
                "action directory changed"
                if str(exc) == "action directory changed"
                else "action repository could not be fetched"
            )
            raise RunError("SETUP_FAILED", message) from exc
        event_name = request.get("event_name") if isinstance(request, dict) else None
        return run_job(
            snapshot,
            pinned["digest"],
            workspace,
            planned["plan"],
            image,
            event,
            owner=owner,
            network=self.network,
            docker_socket=self.docker_socket,
            event_name=event_name,
            node24=(None if self.node24 is None else (self.node24["root"], self.node24["digest"])),
            actions=actions_root,
            runner_image=self.runner_image_digest,
            secrets=(
                None
                if not self.secrets
                else SecretAccess(
                    self.github_repository,
                    self.secret_refs,
                    self.secret_pushers,
                )
            ),
            job_token=self.job_token_config(),
        )

    def _abandoned(self, run_id):
        with self.guard:
            return self.get(run_id)["state"] in TERMINAL

    def _caller_cancel_pending(self, run_id):
        lease = self.live.get(run_id)
        return lease is not None and lease.cancelled()

    def _finish_setup_failure(self, run_id, message):
        while True:
            with self.guard, self.db:
                current = self.get(run_id)
                if current["state"] in TERMINAL:
                    return
                if not self._caller_cancel_pending(run_id):
                    self._publish_artifacts(current)
                    current.update(
                        state="failed",
                        exit_code=None,
                        finished_at=now(),
                        error={"kind": "SETUP_FAILED", "message": message[:512]},
                        cleanup="confirmed_no_external_resources",
                    )
                    self.save(current)
                    self._delete_ownership(current.get("attempt_id"))
                    self._release_finished_attempt(current)
                    return
            # The cancel commit takes the lock after the stop. Wait without
            # holding it so that commit can land before another job starts.
            if self.stop.wait(0.01):
                return

    def _finish_workflow(self, run_id, outcome):
        while True:
            with self.guard, self.db:
                current = self.get(run_id)
                if current["state"] in TERMINAL:
                    return
                if not self._caller_cancel_pending(run_id):
                    self._save_workflow_outcome(run_id, current, outcome)
                    return
            if self.stop.wait(0.01):
                return

    def _save_workflow_outcome(self, run_id, current, outcome):
        missing = self._publish_artifacts(current, outcome.get("uploads"))
        steps = _public_steps(outcome["steps"])
        exit_code = outcome["exit_code"]
        image_ok = outcome["image_digest"] == current["input"]["image_digest"]
        if outcome["status"] == "cancelled" and image_ok:
            current.update(
                state="cancelled",
                exit_code=None,
                error=None,
                cancel_requested=False,
                steps=steps,
            )
        elif missing and outcome["status"] == "succeeded" and exit_code == 0 and image_ok:
            current.update(
                state="failed",
                exit_code=None,
                error={
                    "kind": "STEP_FAILED",
                    "message": f"selected artifact file is missing: {missing[0]}"[:512],
                },
                steps=steps,
            )
        elif outcome["status"] == "succeeded" and exit_code == 0 and image_ok:
            current.update(state="succeeded", exit_code=0, error=None, steps=steps)
        elif type(exit_code) is int and 1 <= exit_code <= 255 and image_ok:
            current.update(state="failed", exit_code=exit_code, error=None, steps=steps)
        else:
            current.update(
                state="failed",
                exit_code=None,
                error={
                    "kind": "SETUP_FAILED" if not image_ok else "STEP_FAILED",
                    "message": _step_failure_message(outcome, image_ok)[:512],
                },
                steps=steps,
            )
        if "node24" in outcome:
            current["node24"] = outcome["node24"]
        stored_revocations = public_token_revocation(outcome.get("token_revocation"))
        if stored_revocations:
            current["token_revocation"] = stored_revocations
        current.update(finished_at=now(), cleanup="confirmed_no_external_resources")
        self.db.execute(
            "UPDATE runs SET log=? WHERE id=?",
            (_workflow_log(outcome["steps"]), run_id),
        )
        self.save(current)
        self._delete_ownership(current.get("attempt_id"))
        self._release_finished_attempt(current)

    def _release_finished_attempt(self, record):
        """Remove a finished attempt directory. The run record stays.

        An unresolved container cleanup keeps its directory. Snapshots stay.
        """

        if record.get("state") not in TERMINAL:
            return
        if record.get("cleanup") != "confirmed_no_external_resources":
            return
        attempt_id = record.get("attempt_id")
        if not _ATTEMPT_ID.fullmatch(attempt_id or ""):
            return
        self._remove_workspace(Path(self.state) / "attempts" / attempt_id)

    def _reclaim_terminal_attempts(self):
        """Drop finished attempt directories so a later submission can fit."""

        with self.db:
            rows = self.db.execute("SELECT record FROM runs").fetchall()
        for (raw,) in rows:
            record = json.loads(raw)
            self._release_finished_attempt(record)

    def _folder_protected(self, record):
        if record.get("state") not in TERMINAL:
            return True
        return record.get("cleanup") != "confirmed_no_external_resources"

    def _folder_names(self, records):
        snapshots = set()
        attempts = set()
        for record in records:
            snapshot_id = (record.get("input") or {}).get("snapshot_id")
            if isinstance(snapshot_id, str) and _ATTEMPT_ID.fullmatch(snapshot_id):
                snapshots.add(snapshot_id)
            attempt_id = record.get("attempt_id")
            if isinstance(attempt_id, str) and _ATTEMPT_ID.fullmatch(attempt_id):
                attempts.add(attempt_id)
        return snapshots, attempts

    def _delete_finished_folders(self, records, drop_ids):
        kept = [record for record in records if record.get("run_id") not in drop_ids]
        dropped = [record for record in records if record.get("run_id") in drop_ids]
        keep_snapshots, keep_attempts = self._folder_names(kept)
        drop_snapshots, drop_attempts = self._folder_names(dropped)
        for name in drop_snapshots - keep_snapshots:
            self._drop_snapshot(name)
        for name in drop_attempts - keep_attempts:
            self._remove_workspace(Path(self.state) / "attempts" / name)

    def _prune_finished_run_folders(self, incoming):
        """Remove finished run folders. In-flight runs and their folders stay.

        A finished folder is past retention at 90 days or beyond the newest
        100. When the disk budget still would not fit `incoming`, the oldest
        remaining finished folders go first. Run records and keys stay.
        """

        records = self._records()
        eligible = []
        for record in records:
            stamp = _finished_stamp(record.get("finished_at"))
            if self._folder_protected(record) or stamp is None:
                continue
            eligible.append((stamp, record))
        eligible.sort(key=lambda item: item[0])
        current = datetime.now(UTC)
        newest = {record["run_id"] for _stamp, record in eligible[-FINISHED_RUN_FOLDER_COUNT:]}
        drop_ids = set()
        keep = []
        for stamp, record in eligible:
            age = (current - stamp).total_seconds()
            expired = age >= FINISHED_RUN_FOLDER_SECONDS or record["run_id"] not in newest
            if expired:
                drop_ids.add(record["run_id"])
            else:
                keep.append(record)
        self._delete_finished_folders(records, drop_ids)
        for record in keep:
            try:
                used = usage(self.state)
            except OSError:
                return
            if used + incoming <= self.disk_budget:
                return
            drop_ids.add(record["run_id"])
            self._delete_finished_folders(records, drop_ids)

    def _remove_workspace(self, workspace):
        attempts = Path(self.state) / "attempts"
        try:
            if workspace.is_symlink() or workspace.parent != attempts or not workspace.is_dir():
                return
            shutil.rmtree(workspace)
        except OSError:
            return

    def _cancel_running_workflow(self, record):
        """Stop the owned container, then commit cancelled only if it is gone.

        The worker lock covers the commit, not the stop. The commit reads the
        record again under that lock and leaves a terminal state that has
        already been committed. A finish that arrives after caller cancel is
        marked does not replace the still-running record. Waiting for the
        container name uses the existing 60 second docker create timeout,
        not a new limit. The stop uses the grace in `_stop_container`.
        The run thread closes the lease after service containers and the
        owned network are gone. Waiting for that close reuses the 60 second
        docker create timeout. It is not a new limit.
        """

        with self.guard:
            current = self.get(record["run_id"])
            if current["state"] in TERMINAL:
                return current
            lease = self.live.get(current["run_id"])
            if lease is None:
                return self._commit_lost(current, None)
            phase, _name, _docker = lease.request_cancel()
            if phase == "idle":
                return self._commit_cancelled(current)
        if phase == "creating":
            lease.wait_until_published(60)
        return self._stop_and_commit(current, lease)

    def _stop_and_commit(self, record, lease):
        if not lease.removed():
            lease.stop()
        if lease.snapshot()[0] != "closed":
            lease.wait_closed(60)
        if not lease.removed():
            lease.stop()
        if lease.removed():
            return self._commit_cancelled(record)
        _phase, name, _docker, _cancel = lease.snapshot()
        return self._commit_lost(record, name)

    def _commit_cancelled(self, record):
        with self.guard, self.db:
            current = self.get(record["run_id"])
            if current["state"] in TERMINAL:
                self.live.pop(current["run_id"], None)
                return current
            self._publish_artifacts(current)
            current.update(
                state="cancelled",
                cancel_requested=True,
                exit_code=None,
                error=None,
                finished_at=now(),
                cleanup="confirmed_no_external_resources",
            )
            self.save(current)
            self.live.pop(current["run_id"], None)
            self._delete_ownership(current.get("attempt_id"))
            self._release_finished_attempt(current)
            return current

    def _commit_lost(self, record, container_name):
        with self.guard, self.db:
            current = self.get(record["run_id"])
            if current["state"] in TERMINAL:
                self.live.pop(current["run_id"], None)
                return current
            self._publish_artifacts(current)
            current.update(
                state="lost",
                cancel_requested=True,
                exit_code=None,
                finished_at=now(),
                error={
                    "kind": "WORKER_INTERRUPTED",
                    "message": "owned container cleanup was not confirmed",
                },
                cleanup="unresolved",
            )
            self.save(current)
            self.live.pop(current["run_id"], None)
            self.unresolved[current.get("attempt_id") or current["run_id"]] = container_name
            return current

    def submit_workflow(self, p):
        fields(
            p,
            ("version", "submission_key", "workflow", "job_id", "event"),
            (
                "image",
                "event_name",
                "activity_type",
                "changed_files",
                "commit_count",
                "diff_unavailable",
            ),
        )
        key = p["submission_key"]
        if not utf8_string(key, 1, 128):
            invalid("submission_key must be a UTF-8 string of 1 to 128 characters")
        if not utf8_string(p["workflow"], 1, 1024) or not utf8_string(p["job_id"], 1, 128):
            invalid("workflow and job_id must be UTF-8 strings within their limits")
        if "event_name" in p and (
            not utf8_string(p["event_name"], 1, 128)
            or "\0" in p["event_name"]
            or "\n" in p["event_name"]
            or "\r" in p["event_name"]
        ):
            invalid("event_name must be a UTF-8 string of 1 to 128 characters")
        if "activity_type" in p and (
            not utf8_string(p["activity_type"], 1, 128)
            or "\0" in p["activity_type"]
            or "\n" in p["activity_type"]
            or "\r" in p["activity_type"]
        ):
            invalid("activity_type must be a UTF-8 string of 1 to 128 characters")
        if "changed_files" in p:
            files = p["changed_files"]
            if not isinstance(files, list) or len(files) > MAX_CHANGED_FILES:
                invalid("changed_files must be a list of at most 10000 paths")
            for path in files:
                if (
                    not utf8_string(path, 1, MAX_PATH_LENGTH)
                    or "\0" in path
                    or "\n" in path
                    or "\r" in path
                    or path.startswith("/")
                ):
                    invalid("changed_files entries must be relative paths")
        if "commit_count" in p:
            integer(p["commit_count"], 0, 1_000_000_000, "commit_count")
        if "diff_unavailable" in p and type(p["diff_unavailable"]) is not bool:
            invalid("diff_unavailable must be a boolean")
        if "image" in p:
            if not isinstance(p["image"], str) or not (
                _IMAGE_ID.fullmatch(p["image"]) or _IMAGE_REF.fullmatch(p["image"])
            ):
                invalid("image is not pinned by digest")
        elif self.runner_image is None:
            invalid("image is not pinned by digest")
        try:
            event_text = canonical(p["event"])
        except (TypeError, ValueError, UnicodeError):
            invalid("event input is not accepted")
        submitted = {
            "version": 1,
            "workflow": p["workflow"],
            "job_id": p["job_id"],
            "event": p["event"],
        }
        if "image" in p:
            submitted["image"] = p["image"]
        if "event_name" in p:
            submitted["event_name"] = p["event_name"]
        normalized = canonical(submitted)
        existing = self.db.execute(
            "SELECT request, record FROM runs WHERE submission_key=?", (key,)
        ).fetchone()
        if existing:
            if existing[0] != normalized:
                raise Fault(
                    "IDEMPOTENCY_CONFLICT", "submission key already identifies different inputs"
                )
            return json.loads(existing[1])
        if self.stop.is_set() or self.execution_error is not None:
            raise Fault(
                "WORKER_NOT_READY",
                "worker cannot accept new execution; inspect worker.describe",
            )
        # This process refuses every new workflow attempt while its unresolved
        # map is non-empty. The same key still returns its original run above.
        # Development fixtures do not use this gate, and describe.ready stays
        # the scheduler flag. Restart does not keep this map. It blocks only an
        # attempt that would reuse a retained container name or workspace.
        if conflicts_with_unresolved(self.unresolved):
            raise Fault(
                "WORKER_NOT_READY",
                "an unresolved owned container blocks a new attempt",
            )
        try:
            capture = SourceCapture(self.repository, self.state)
            with capture.prepare(p["workflow"]) as prepared:
                try:
                    if p.get("event_name") in (
                        "push",
                        "pull_request",
                    ) and not self._event_triggered(p, prepared.path):
                        return {"triggered": False}
                except Fault:
                    raise
                queued = self.db.execute(
                    "SELECT count(*) FROM runs WHERE json_extract(record, '$.state')='queued'"
                ).fetchone()[0]
                if queued >= MAX_QUEUE:
                    raise Fault("QUEUE_FULL", "queued run limit reached")
                store = self.action_store()
                with store.guard:
                    return self._plan_and_accept(p, key, normalized, event_text, prepared, store)
        except CaptureError as exc:
            self._capture_fault(exc)

    def _event_triggered(self, p, snapshot):
        try:
            on = workflow_on(snapshot_workflow_bytes(snapshot))
        except PlanError as exc:
            raise self._plan_fault(exc) from exc
        except (OSError, UnicodeError, ValueError):
            raise Fault("INVALID_PARAMS", "workflow snapshot could not be planned") from None
        return submission_triggered(
            on,
            p["event_name"],
            p["event"],
            p.get("activity_type"),
            p.get("changed_files", []),
            p.get("commit_count"),
            p.get("diff_unavailable", False),
        )

    def _plan_and_accept(self, p, key, normalized, event_text, prepared, store):
        """Plan once, copy history before publish, then accept that plan."""

        try:
            store.begin()
        except OSError as exc:
            raise Fault("INVALID_PARAMS", "workflow snapshot could not be planned") from exc
        try:
            try:
                planned = plan_snapshot(prepared.path, p["job_id"], action_store=store)
            except PlanError as exc:
                raise self._plan_fault(exc) from exc
            except (OSError, UnicodeError, ValueError) as exc:
                raise Fault("INVALID_PARAMS", "workflow snapshot could not be planned") from exc
            if plan_needs_history(planned["plan"]):
                try:
                    prepared.add_history()
                except CaptureError as exc:
                    self._capture_fault(exc)
            captured = prepared.publish()
            snapshot = Path(self.state) / "snapshots" / captured["snapshot_id"]
            return self._accept_planned(
                p, key, normalized, event_text, snapshot, captured, store, planned
            )
        finally:
            if store.is_open:
                store.rollback()

    def _accept_planned(
        self, p, key, normalized, event_text, snapshot, captured, store, planned=None
    ):
        if planned is None:
            try:
                store.begin()
            except OSError as exc:
                self._drop_snapshot(captured["snapshot_id"])
                raise Fault("INVALID_PARAMS", "workflow snapshot could not be planned") from exc
        try:
            try:
                manifest = verify_snapshot(snapshot, captured["digest"])
                if planned is None:
                    planned = plan_snapshot(snapshot, p["job_id"], action_store=store)
            except VerifyError as exc:
                raise Fault("INTERNAL_ERROR", "captured snapshot failed verification") from exc
            except PlanError as exc:
                raise self._plan_fault(exc) from exc
            except (OSError, UnicodeError, ValueError) as exc:
                raise Fault("INVALID_PARAMS", "workflow snapshot could not be planned") from exc
            if "image" in p:
                image = p["image"]
            else:
                _require_ubuntu_latest(planned["plan"])
                image = self.runner_image
            image_digest = "sha256:" + image.rsplit("sha256:", 1)[1]
            record = {
                "run_id": str(uuid.uuid4()),
                "worker_id": self.worker_id,
                "submission_key": key,
                "state": "queued",
                "exit_code": None,
                "input": {
                    "kind": "workflow_job",
                    "digest": captured["digest"],
                    "snapshot_id": captured["snapshot_id"],
                    "workflow": manifest["workflow"],
                    "workflow_digest": captured["workflow_digest"],
                    "plan_digest": planned["digest"],
                    "job_id": p["job_id"],
                    "event_digest": hashlib.sha256(event_text.encode("ascii")).hexdigest(),
                    "image_digest": image_digest,
                    "image_reference": image,
                },
                "backend": {"name": "workflow", "version": __version__},
                "compatibility_notes": [
                    "The selected closure runs one job at a time in one caller-pinned container. Matrix combinations and reusable workflows share that container and run one at a time.",
                    "Step if, job needs, job outputs, environment files, local composites, job matrices, and reusable workflows are evaluated. Secrets are not passed. A node24 main and post run. pre, node20, and Docker are unclaimed. Services do not receive the engine socket.",
                    "A remote action pinned by a 40-character commit SHA is fetched with Git and no credential. Composite run steps execute. A node24 main and its post run from a copy in the attempt. pre, node20, and Docker stay rejected.",
                ],
                "accepted_at": now(),
                "started_at": None,
                "finished_at": None,
                "attempt_id": None,
                "cancel_requested": False,
                "error": None,
                "cleanup": "not_started",
            }
            actions = remote_action_records(planned["plan"])
            if actions:
                record["input"]["actions"] = actions
            try:
                groups = resolve_groups(
                    planned["plan"], p["event"], p.get("event_name"), p["workflow"]
                )
            except ExprError as exc:
                raise Fault("INVALID_PARAMS", str(exc)[:512]) from exc
            if groups:
                record["concurrency"] = groups
                record["compatibility_notes"] = [
                    *record["compatibility_notes"],
                    _CONCURRENCY_NOTE,
                ]
            try:
                reserve = usage(snapshot)
            except OSError:
                raise Fault("STORAGE_FULL", _STORAGE_FULL) from None
            # usage(state) already includes this snapshot and any action
            # stored for it. Reserve the same snapshot size again for the
            # later attempt workspace, plus the queued row.
            reserve += len(normalized.encode()) + len(canonical(record).encode())
            if self._over_budget(reserve):
                raise Fault("STORAGE_FULL", _STORAGE_FULL)
            running_ids = []
            try:
                with self.db:
                    self.db.execute(
                        "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                        (record["run_id"], key, normalized, canonical(record), b""),
                    )
                    if groups:
                        running_ids = self._apply_concurrency(record)
            except sqlite3.IntegrityError:
                existing = self.db.execute(
                    "SELECT request, record FROM runs WHERE submission_key=?", (key,)
                ).fetchone()
                if existing and existing[0] == normalized:
                    return json.loads(existing[1])
                raise Fault(
                    "IDEMPOTENCY_CONFLICT", "submission key already identifies different inputs"
                ) from None
            store.commit()
            self._pending_concurrency_cancels.extend(running_ids)
            return record
        finally:
            if store.is_open:
                self._drop_snapshot(captured["snapshot_id"])
                store.rollback()

    @staticmethod
    def _capture_fault(exc):
        message = str(exc)[:512]
        if exc.kind == "CAPABILITY_UNSUPPORTED":
            raise Fault("CAPABILITY_UNSUPPORTED", message) from exc
        if exc.kind == "SOURCE_UNSTABLE":
            raise Fault("SOURCE_UNSTABLE", message) from exc
        raise Fault("INVALID_PARAMS", message) from exc

    def action_store(self):
        if self._action_store is None:
            self._action_store = ActionStore(
                self.state, self.action_remote, lambda: self.disk_budget
            )
        return self._action_store

    @staticmethod
    def _plan_fault(exc):
        if exc.kind == "CAPABILITY_UNSUPPORTED":
            return Fault("CAPABILITY_UNSUPPORTED", str(exc)[:512])
        if exc.kind == "ACTION_UNAVAILABLE":
            return Fault("ACTION_UNAVAILABLE", str(exc)[:512])
        if exc.kind == "STORAGE_FULL":
            return Fault("STORAGE_FULL", _STORAGE_FULL)
        return Fault("INVALID_PARAMS", str(exc)[:512])

    def _over_budget(self, incoming):
        try:
            usage(self.state)
        except OSError:
            return True
        self._reclaim_terminal_attempts()
        self._prune_finished_run_folders(incoming)
        try:
            used = usage(self.state)
        except OSError:
            return True
        return used + incoming > self.disk_budget

    def _publish_artifacts(self, record, selections=None):
        """Record files this attempt wrote. A confirmed terminal run then removes the directory.

        `selections` None reads `uploads.json` beside the workspace. A missing
        or unusable file means no names. Paths that are no longer regular
        files are returned and are not inserted.
        """

        if (record.get("input") or {}).get("kind") != "workflow_job":
            return []
        attempt_id = record.get("attempt_id")
        if not attempt_id or not _ATTEMPT_ID.fullmatch(attempt_id):
            return []
        if self.db.execute(
            "SELECT 1 FROM artifacts WHERE run_id=? LIMIT 1", (record["run_id"],)
        ).fetchone():
            return []
        workspace = self.state / "attempts" / attempt_id / "workspace"
        snapshot_id = record["input"].get("snapshot_id")
        if not isinstance(snapshot_id, str) or not _ATTEMPT_ID.fullmatch(snapshot_id):
            return []
        snapshot = self.state / "snapshots" / snapshot_id
        if selections is None:
            selections = load_uploads(workspace.parent / "uploads.json")
        try:
            if (
                workspace.is_symlink()
                or not workspace.is_dir()
                or snapshot.is_symlink()
                or not snapshot.is_dir()
            ):
                return []
            entries, missing = named_manifest(workspace, snapshot, selections)
        except OSError:
            return []
        for entry in entries:
            self.db.execute(
                "INSERT INTO artifacts(id, run_id, path, size, digest, name) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    record["run_id"],
                    entry["path"],
                    entry["size"],
                    entry["digest"],
                    entry.get("name"),
                ),
            )
        return missing

    def _drop_snapshot(self, snapshot_id):
        target = Path(self.state) / "snapshots" / snapshot_id
        if target.is_symlink() or not target.is_dir():
            return
        shutil.rmtree(target)

    def dispatch(self, method, p):
        if method == "worker.describe":
            fields(p)
            described = {
                "protocol_versions": [0, 1],
                "worker_id": self.worker_id,
                "repository": self.repository,
                "version": __version__,
                "ready": not self.stop.is_set() and self.execution_error is None,
                "readiness_error": self.execution_error,
                "methods": METHODS,
                "capabilities": [
                    "development.fixture",
                    "run.cancel",
                    "run.logs",
                    "workflow.job",
                ],
                "limits": {
                    "message_bytes": MAX_MESSAGE,
                    "log_page_bytes": MAX_LOG_PAGE,
                    "queued_runs": MAX_QUEUE,
                    "list_page": MAX_LIST_PAGE,
                    "cursor_characters": MAX_CURSOR,
                    "request_id_characters": 128,
                    "fixture_output_bytes": MAX_LOG_PAGE,
                    "fixture_delay_ms": 5000,
                    "submission_key_characters": 128,
                    "json_depth": MAX_JSON_DEPTH,
                },
                "retention": "runs and submission keys retained indefinitely; terminal attempt directories removed",
            }
            if self.node24 is not None:
                described["node24"] = {
                    "digest": self.node24["digest"],
                    "mount": _NODE24_MOUNT,
                }
            if self.runner_image_digest is not None:
                described["runner_image"] = self.runner_image_digest
            return described
        if method == "run.submit":
            if isinstance(p, dict) and is_integer(p.get("version")) and int(p["version"]) == 1:
                return self.submit_workflow(p)
            fields(p, ("version", "submission_key", "backend", "fixture"))
            self.version(p)
            if p["backend"] != "development":
                raise Fault(
                    "CAPABILITY_UNSUPPORTED", "only explicit development fixtures are supported"
                )
            key = p["submission_key"]
            if not utf8_string(key, 1, 128):
                invalid("submission_key must be a UTF-8 string of 1 to 128 characters")
            fixture = p["fixture"]
            fields(fixture, (), ("exit_code", "delay_ms", "output"))
            fixture = {
                "exit_code": integer(fixture.get("exit_code", 0), 0, 255, "exit_code"),
                "delay_ms": integer(fixture.get("delay_ms", 0), 0, 5000, "delay_ms"),
                "output": fixture.get("output", "development fixture completed\n"),
            }
            if (
                not utf8_string(fixture["output"], 0, MAX_LOG_PAGE)
                or len(fixture["output"].encode()) > MAX_LOG_PAGE
            ):
                invalid("output must be UTF-8 text of at most 65536 bytes")
            normalized = canonical({"backend": "development", "fixture": fixture})
            existing = self.db.execute(
                "SELECT request, record FROM runs WHERE submission_key=?", (key,)
            ).fetchone()
            if existing:
                if existing[0] != normalized:
                    raise Fault(
                        "IDEMPOTENCY_CONFLICT", "submission key already identifies different inputs"
                    )
                return json.loads(existing[1])
            if self.stop.is_set() or self.execution_error is not None:
                raise Fault(
                    "WORKER_NOT_READY",
                    "worker cannot accept new execution; inspect worker.describe",
                )
            queued = self.db.execute(
                "SELECT count(*) FROM runs WHERE json_extract(record, '$.state')='queued'"
            ).fetchone()[0]
            if queued >= MAX_QUEUE:
                raise Fault("QUEUE_FULL", "queued run limit reached")
            digest = hashlib.sha256(normalized.encode()).hexdigest()
            record = {
                "run_id": str(uuid.uuid4()),
                "worker_id": self.worker_id,
                "submission_key": key,
                "state": "queued",
                "exit_code": None,
                "input": {"kind": "development_fixture", "digest": digest, "snapshot_id": digest},
                "backend": {"name": "development", "version": __version__},
                "compatibility_notes": [
                    "Synthetic fixture only; no repository source or workflow execution."
                ],
                "accepted_at": now(),
                "started_at": None,
                "finished_at": None,
                "attempt_id": None,
                "cancel_requested": False,
                "error": None,
                "cleanup": "not_started",
            }
            incoming = (
                len(normalized.encode())
                + len(canonical(record).encode())
                + len(fixture["output"].encode())
            )
            if self._over_budget(incoming):
                raise Fault("STORAGE_FULL", _STORAGE_FULL)
            with self.db:
                self.db.execute(
                    "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                    (record["run_id"], key, normalized, canonical(record), b""),
                )
            return record
        if method == "run.get":
            fields(p, ("run_id",))
            return self.get(p["run_id"])
        if method == "run.cancel":
            fields(p, ("version", "run_id"))
            self.version(p)
            with self.guard:
                record = self.get(p["run_id"])
                if record["state"] in TERMINAL:
                    return record
                if record["state"] == "queued" or record["input"]["kind"] != "workflow_job":
                    return self._commit_cancelled(record)
            return self._cancel_running_workflow(record)
        if method == "run.list":
            fields(p, (), ("cursor", "limit", "state"))
            limit = integer(p.get("limit", 20), 1, MAX_LIST_PAGE, "limit")
            state_filter = p.get("state")
            if state_filter is not None and (
                not isinstance(state_filter, str)
                or state_filter not in TERMINAL | {"queued", "running"}
            ):
                invalid("unknown state filter")
            identity = [self.worker_id, state_filter]
            offset = offset_from(p["cursor"], "runs", identity) if "cursor" in p else 0
            rows = self.db.execute(
                "SELECT sequence, record FROM runs WHERE sequence>? AND (? IS NULL OR json_extract(record, '$.state')=?) ORDER BY sequence LIMIT ?",
                (offset, state_filter, state_filter, limit + 1),
            ).fetchall()
            return {
                "runs": [json.loads(r[1]) for r in rows[:limit]],
                "next_cursor": cursor("runs", identity, rows[limit - 1][0])
                if len(rows) > limit
                else None,
            }
        if method == "run.logs":
            fields(p, ("run_id",), ("cursor", "limit"))
            record = self.get(p["run_id"])
            limit = integer(p.get("limit", MAX_LOG_PAGE), 1, MAX_LOG_PAGE, "limit")
            offset = offset_from(p["cursor"], "logs", record["run_id"]) if "cursor" in p else 0
            size = self.db.execute(
                "SELECT length(log) FROM runs WHERE id=?", (record["run_id"],)
            ).fetchone()[0]
            if offset > size:
                raise Fault("CURSOR_EXPIRED", "cursor exceeds available output")
            data = (
                self.db.execute(
                    "SELECT substr(log, ?, ?) FROM runs WHERE id=?",
                    (offset + 1, limit, record["run_id"]),
                ).fetchone()[0]
                or b""
            )
            return {
                "data_base64": base64.b64encode(data).decode(),
                "next_cursor": cursor("logs", record["run_id"], offset + len(data)),
                "end_of_stream": record["state"] in TERMINAL and offset + len(data) == size,
            }
        if method == "run.artifacts":
            fields(p, ("run_id",), ("cursor", "limit"))
            record = self.get(p["run_id"])
            if record["input"]["kind"] != "workflow_job":
                raise Fault(
                    "CAPABILITY_UNSUPPORTED", "development fixtures do not publish artifacts"
                )
            limit = integer(p.get("limit", MAX_LIST_PAGE), 1, MAX_LIST_PAGE, "limit")
            offset = offset_from(p["cursor"], "artifacts", record["run_id"]) if "cursor" in p else 0
            total = self.db.execute(
                "SELECT count(*) FROM artifacts WHERE run_id=?", (record["run_id"],)
            ).fetchone()[0]
            if offset > total:
                raise Fault("CURSOR_EXPIRED", "cursor exceeds available artifacts")
            rows = self.db.execute(
                "SELECT id, path, size, digest, name FROM artifacts WHERE run_id=? ORDER BY path LIMIT ? OFFSET ?",
                (record["run_id"], limit + 1, offset),
            ).fetchall()
            page = rows[:limit]
            artifacts = []
            for row in page:
                item = {"id": row[0], "path": row[1], "size": row[2], "digest": row[3]}
                if row[4] is not None:
                    item["name"] = row[4]
                artifacts.append(item)
            return {
                "artifacts": artifacts,
                "next_cursor": cursor("artifacts", record["run_id"], offset + len(page))
                if len(rows) > limit
                else None,
            }
        if method == "artifact.read":
            fields(p, ("artifact_id",), ("offset", "limit"))
            artifact_id = p["artifact_id"]
            if not _ATTEMPT_ID.fullmatch(artifact_id or ""):
                invalid("artifact_id must be a UUID")
            row = self.db.execute(
                "SELECT run_id, path, size, digest FROM artifacts WHERE id=?",
                (artifact_id,),
            ).fetchone()
            if not row:
                invalid("artifact does not exist")
            offset = integer(p.get("offset", 0), 0, MAX_OFFSET, "offset")
            limit = integer(p.get("limit", MAX_LOG_PAGE), 1, MAX_LOG_PAGE, "limit")
            record = self.get(row[0])
            if record["input"]["kind"] != "workflow_job" or not record.get("attempt_id"):
                raise Fault(
                    "CAPABILITY_UNSUPPORTED", "development fixtures do not publish artifacts"
                )
            workspace = self.state / "attempts" / record["attempt_id"] / "workspace"
            try:
                size, digest = file_identity(workspace, row[1])
                if size != row[2] or digest != row[3]:
                    raise ArtifactError()
                if offset > size:
                    invalid("offset exceeds artifact size")
                data, next_offset, end = read_bytes(workspace, row[1], offset, limit)
            except ArtifactError:
                raise Fault("INTERNAL_ERROR", "artifact bytes are not available") from None
            return {
                "data_base64": base64.b64encode(data).decode(),
                "next_offset": next_offset,
                "end_of_stream": end,
            }
        if method == "run.status":
            return self.report_status(p)
        raise Fault("METHOD_NOT_FOUND", "unknown method")

    def report_status(self, p):
        """Decide or record one commit status. The credential is not a parameter.

        Optional check fields record the check-run id and the mapped status
        and conclusion. They are not plan fields. `checks` asks for that
        mapping on a decision. A repeated terminal conclusion is a skip.
        """

        fields(
            p,
            ("run_id", "tested_commit", "status_sha", "context"),
            ("record", "checks", "check_run_id", "check_status", "check_conclusion"),
        )
        tested = p["tested_commit"]
        status_sha = p["status_sha"]
        context = p["context"]
        if not isinstance(tested, str) or not _STATUS_SHA.fullmatch(tested):
            invalid("tested_commit must be 40 or 64 lowercase hex characters")
        if not isinstance(status_sha, str) or not _STATUS_SHA.fullmatch(status_sha):
            invalid("status_sha must be 40 or 64 lowercase hex characters")
        if not utf8_string(context, 1, MAX_CONTEXT_LENGTH) or any(
            character in context for character in "\0\r\n"
        ):
            invalid("context must be one line of at most 1024 characters")
        if "checks" in p and p["checks"] is not True:
            invalid("checks is not accepted")
        recording_check = "check_run_id" in p or "check_status" in p or "check_conclusion" in p
        if p.get("checks") and ("record" in p or recording_check):
            invalid("missing or unknown parameters")
        if recording_check and ("check_run_id" not in p or "check_status" not in p):
            invalid("missing or unknown parameters")
        if "check_conclusion" in p and "check_run_id" not in p:
            invalid("missing or unknown parameters")
        record = self.get(p["run_id"])
        if record["input"].get("kind") != "workflow_job":
            raise Fault("STATUS_REFUSED", "run is not a workflow run")
        manifest = self._status_manifest(record)
        if manifest["dirty"] is not False:
            raise Fault("STATUS_REFUSED", "snapshot is dirty")
        included = manifest["included"]
        if not isinstance(included, list) or included:
            raise Fault("STATUS_REFUSED", "snapshot includes files")
        if manifest["base_commit"] != tested:
            raise Fault("STATUS_REFUSED", "base_commit does not equal the tested commit")
        state = github_state(record["state"], record["exit_code"])
        check_status, check_conclusion = check_mapping(record["state"], record["exit_code"])
        posted = p.get("record")
        if posted is None and not recording_check:
            if p.get("checks"):
                return self._check_decision(
                    record, context, status_sha, state, check_status, check_conclusion
                )
            if state in TERMINAL_STATUS and self._status_recorded(
                record["run_id"], context, status_sha, state
            ):
                return {"action": "skip", "state": state}
            return {"action": "post", "state": state}
        if posted is not None and (
            posted != state or posted not in {"pending", "success", "failure", "error"}
        ):
            invalid("recorded state does not match the run")
        if recording_check:
            self._accept_check_record(p, check_status, check_conclusion)
        with self.db:
            if recording_check:
                self.db.execute(
                    """
                    INSERT INTO check_posts(
                        run_id, context, sha, check_run_id, status, conclusion
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, context, sha) DO UPDATE SET
                        check_run_id=excluded.check_run_id,
                        status=excluded.status,
                        conclusion=excluded.conclusion
                    """,
                    (
                        record["run_id"],
                        context,
                        status_sha,
                        int(p["check_run_id"]),
                        p["check_status"],
                        p.get("check_conclusion"),
                    ),
                )
            if posted is not None:
                self.db.execute(
                    "INSERT OR IGNORE INTO status_posts(run_id, context, sha, state) "
                    "VALUES (?, ?, ?, ?)",
                    (record["run_id"], context, status_sha, posted),
                )
        return {"action": "recorded", "state": state}

    def _accept_check_record(self, p, check_status, check_conclusion):
        identifier = integer(p["check_run_id"], 1, 2**63 - 1, "check_run_id")
        p["check_run_id"] = identifier
        if p["check_status"] not in {"queued", "in_progress", "completed"}:
            invalid("recorded check does not match the run")
        if p["check_status"] != check_status:
            invalid("recorded check does not match the run")
        recorded_conclusion = p.get("check_conclusion", None)
        if check_conclusion is None:
            if "check_conclusion" in p:
                invalid("recorded check does not match the run")
        elif recorded_conclusion != check_conclusion:
            invalid("recorded check does not match the run")
        if recorded_conclusion is not None and recorded_conclusion not in {
            "success",
            "failure",
            "cancelled",
        }:
            invalid("recorded check does not match the run")

    def _check_decision(self, record, context, status_sha, state, check_status, check_conclusion):
        row = self.db.execute(
            "SELECT check_run_id, conclusion FROM check_posts WHERE run_id=? AND context=? AND sha=?",
            (record["run_id"], context, status_sha),
        ).fetchone()
        if (
            check_conclusion in {"success", "failure", "cancelled"}
            and row is not None
            and row["conclusion"] == check_conclusion
        ):
            return {"action": "skip", "state": state}
        result = {
            "action": "post",
            "state": state,
            "check_status": check_status,
            "check_summary": check_summary(record["state"], record["exit_code"]),
        }
        if check_conclusion is not None:
            result["check_conclusion"] = check_conclusion
        if row is not None:
            result["check_run_id"] = row["check_run_id"]
        if state in TERMINAL_STATUS and self._status_recorded(
            record["run_id"], context, status_sha, state
        ):
            result["status_recorded"] = True
        return result

    def _status_manifest(self, record):
        snapshot_id = record["input"].get("snapshot_id")
        digest = record["input"].get("digest")
        if not isinstance(snapshot_id, str) or not _ATTEMPT_ID.fullmatch(snapshot_id):
            raise Fault("STATUS_REFUSED", "snapshot is not readable")
        snapshot = self.state / "snapshots" / snapshot_id
        try:
            if snapshot.is_symlink() or not snapshot.is_dir():
                raise Fault("STATUS_REFUSED", "snapshot is not readable")
            return verify_snapshot(snapshot, digest)
        except (VerifyError, OSError, UnicodeError, ValueError):
            raise Fault("STATUS_REFUSED", "snapshot is not readable") from None

    def _status_recorded(self, run_id, context, sha, state):
        row = self.db.execute(
            "SELECT 1 FROM status_posts WHERE run_id=? AND context=? AND sha=? AND state=?",
            (run_id, context, sha, state),
        ).fetchone()
        return row is not None

    @staticmethod
    def version(p):
        if not is_integer(p["version"]) or p["version"] != 0:
            raise Fault("VERSION_UNSUPPORTED", "only protocol version 0 is supported")

    def response(self, raw):
        request_id = None
        try:
            try:
                request = strict_json(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise Fault(
                    "PARSE_ERROR", "invalid UTF-8 JSON, duplicate keys, or excessive nesting"
                ) from None
            if (
                not isinstance(request, dict)
                or request.get("jsonrpc") != "2.0"
                or not utf8_string(request.get("method"), 1, 128)
                or "id" not in request
                or not valid_id(request["id"])
                or set(request) - {"jsonrpc", "id", "method", "params"}
            ):
                raise Fault(
                    "INVALID_REQUEST", "expected bounded JSON-RPC request with string or integer id"
                )
            request_id = request["id"]
            method = request["method"]
            params = request.get("params", {})
            # run.cancel stops a container outside this lock. Its commit takes
            # the lock again and keeps a terminal result that landed mid-stop.
            if method == "run.cancel":
                result = self.dispatch(method, params)
            elif method == "run.submit":
                with self.guard:
                    result = self.dispatch(method, params)
                    pending = list(self._pending_concurrency_cancels)
                    self._pending_concurrency_cancels = []
                for run_id in pending:
                    self._cancel_running_workflow({"run_id": run_id})
            else:
                with self.guard:
                    result = self.dispatch(method, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Fault as error:
            return error_response(request_id, error)
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                return error_response(
                    request_id,
                    Fault("STORAGE_FULL", _STORAGE_FULL),
                )
            return error_response(
                request_id, Fault("INTERNAL_ERROR", "worker storage operation failed")
            )
        except Exception:
            # Never include database paths, query text, or submitted contents in errors.
            return error_response(
                request_id, Fault("INTERNAL_ERROR", "worker could not process request")
            )

    def serve(self):
        self.start()
        try:
            while not self.stop.is_set():
                try:
                    client, _ = self.server.accept()
                except TimeoutError:
                    continue
                with client:
                    client.settimeout(1)
                    try:
                        with client.makefile("rb") as stream:
                            raw = stream.readline(MAX_MESSAGE + 1)
                        if len(raw) > MAX_MESSAGE or not raw.endswith(b"\n"):
                            reply = error_response(
                                None,
                                Fault(
                                    "INVALID_REQUEST",
                                    "expected newline-delimited request within 1 MiB",
                                ),
                            )
                        else:
                            reply = self.response(raw)
                        client.sendall((canonical(reply) + "\n").encode())
                    except (OSError, ValueError):
                        # A lost reply does not roll back an accepted submission.
                        pass
        finally:
            self.close()


def _accept_runner_image(value):
    if value is None:
        return None
    if not isinstance(value, str) or not (
        _IMAGE_ID.fullmatch(value) or _IMAGE_REF.fullmatch(value)
    ):
        raise ValueError("runner image is not pinned by digest")
    return value


def _require_ubuntu_latest(plan):
    """Reject an omitted image unless every selected job is ubuntu-latest."""

    def walk(jobs):
        if not isinstance(jobs, list) or not jobs:
            raise Fault("CAPABILITY_UNSUPPORTED", "runs-on is not ubuntu-latest")
        for job in jobs:
            if not isinstance(job, dict):
                raise Fault("CAPABILITY_UNSUPPORTED", "runs-on is not ubuntu-latest")
            call = job.get("call")
            if isinstance(call, dict):
                walk(call.get("jobs"))
                continue
            if job.get("runs_on") != "ubuntu-latest":
                raise Fault("CAPABILITY_UNSUPPORTED", "runs-on is not ubuntu-latest")

    if not isinstance(plan, dict):
        raise Fault("CAPABILITY_UNSUPPORTED", "runs-on is not ubuntu-latest")
    walk(plan.get("jobs"))


def serve(
    repository,
    state,
    disk_budget=None,
    network=DEFAULT_NETWORK,
    docker_socket=False,
    node24=None,
    runner_image=None,
    *,
    secrets=False,
    github_repository=None,
    app_key=None,
    secret_refs=None,
    secret_pushers=None,
    api_base=None,
):
    worker = Worker(
        repository,
        state,
        disk_budget,
        network=network,
        docker_socket=docker_socket,
        node24=node24,
        runner_image=runner_image,
        secrets=secrets,
        github_repository=github_repository,
        app_key=app_key,
        secret_refs=secret_refs,
        secret_pushers=secret_pushers,
        api_base=api_base,
    )
    if worker.host_control_warning:
        print(  # noqa: T201  host-control warning on stderr
            worker.host_control_warning,
            file=sys.stderr,
            flush=True,
        )
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop.set())
    worker.serve()
