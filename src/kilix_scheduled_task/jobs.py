"""Task definitions, where they live, and their run history.

The definitions file is the source of truth; systemd units and crontab lines
are generated from it (see `backends`). It is changed the same way as
kilix-todo's file: an exclusive lock, a new file written beside it, synced and
renamed into place.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .schedule import Schedule, ScheduleError

__all__ = ["Job", "JobError", "Jobs", "Paths", "Store", "read_runs"]

SCHEMA = "kilix.scheduled-task/v1"
MAX_NAME = 80
MAX_COMMAND = 8000


class JobError(ValueError):
    pass


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Paths:
    """Every place the program reads or writes."""

    jobs: Path        # the definitions
    state: Path       # run history and output, one directory per task
    data: Path        # the installed runner
    units: Path       # systemd user units

    @classmethod
    def default(cls) -> "Paths":
        home = Path.home()
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
        data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
        jobs = os.environ.get("KILIX_SCHEDULED_TASK_FILE")
        return cls(jobs=Path(jobs) if jobs else config / "kilix-scheduled-task" / "tasks.json",
                   state=state / "kilix-scheduled-task",
                   data=data / "kilix-scheduled-task",
                   units=config / "systemd" / "user")

    @property
    def runner(self) -> Path:
        return self.data / "kst-run.py"

    def task_dir(self, job_id: str) -> Path:
        return self.state / job_id


def slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return text[:32].strip("-") or "task"


@dataclass
class Job:
    id: str
    name: str
    command: str
    schedule: Schedule
    enabled: bool = True
    cwd: str = "~"
    catch_up: bool = True
    notify_failure: bool = False
    timeout: int = 0          # seconds; 0 is no limit
    keep: int = 20            # output logs kept
    created: str = field(default_factory=now_stamp)

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", self.id or ""):
            raise JobError(f"bad task id {self.id!r}")
        self.name = " ".join(str(self.name).split())[:MAX_NAME]
        if not self.name:
            raise JobError("a task needs a name")
        self.command = str(self.command).strip()
        if not self.command:
            raise JobError("a task needs a command")
        if "\0" in self.command or len(self.command) > MAX_COMMAND:
            raise JobError("the command is not usable (NUL byte or too long)")
        self.cwd = str(self.cwd).strip() or "~"
        if not (self.cwd == "~" or self.cwd.startswith("~/") or self.cwd.startswith("/")):
            raise JobError("the working directory must be absolute or start with ~")
        self.enabled = bool(self.enabled)
        self.catch_up = bool(self.catch_up)
        self.notify_failure = bool(self.notify_failure)
        self.timeout = max(0, int(self.timeout))
        self.keep = max(1, min(1000, int(self.keep)))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "command": self.command,
                "schedule": self.schedule.to_dict(), "enabled": self.enabled,
                "cwd": self.cwd, "catch_up": self.catch_up,
                "notify_failure": self.notify_failure, "timeout": self.timeout,
                "keep": self.keep, "created": self.created}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        if not isinstance(data, dict):
            raise JobError("a task is not an object")
        try:
            job = cls(id=data["id"], name=data["name"], command=data["command"],
                      schedule=Schedule.from_dict(data.get("schedule") or {}),
                      enabled=data.get("enabled", True), cwd=data.get("cwd", "~"),
                      catch_up=data.get("catch_up", True),
                      notify_failure=data.get("notify_failure", False),
                      timeout=data.get("timeout", 0), keep=data.get("keep", 20),
                      created=data.get("created") or now_stamp())
            job.validate()
        except (KeyError, TypeError, ValueError, ScheduleError) as error:
            raise JobError(f"bad task {data.get('id')!r}: {error}") from None
        return job

    @property
    def directory(self) -> str:
        return os.path.expanduser(self.cwd)


@dataclass
class Jobs:
    jobs: list[Job] = field(default_factory=lambda: [])
    revision: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Jobs":
        if data.get("schema") != SCHEMA:
            raise JobError(f"not a {SCHEMA} file (schema {data.get('schema')!r})")
        raw = data.get("jobs", [])
        if not isinstance(raw, list):
            raise JobError("jobs must be an array")
        jobs = [Job.from_dict(j) for j in raw]
        ids = [j.id for j in jobs]
        if len(ids) != len(set(ids)):
            raise JobError("duplicate task ids")
        try:
            revision = int(data.get("revision", 0))
        except (TypeError, ValueError):
            raise JobError("revision must be a number") from None
        return cls(jobs, revision)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "revision": self.revision,
                "jobs": [j.to_dict() for j in self.jobs]}

    def get(self, key: str) -> Job:
        """By id, or by a name or id prefix when that is unambiguous."""
        for job in self.jobs:
            if job.id == key:
                return job
        lowered = key.lower()
        matches = [j for j in self.jobs if j.name.lower() == lowered] or \
            [j for j in self.jobs if j.id.startswith(lowered)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise JobError(f"{key!r} matches {', '.join(j.id for j in matches)}")
        raise JobError(f"no task {key!r}")

    def new_id(self, name: str) -> str:
        base = slug(name)
        taken = {j.id for j in self.jobs}
        candidate, n = base, 2
        while candidate in taken:
            suffix = f"-{n}"
            candidate = base[:40 - len(suffix)] + suffix
            n += 1
        return candidate

    def add(self, name: str, command: str, schedule: Schedule, **options: Any) -> Job:
        job = Job(id=self.new_id(name), name=name, command=command,
                  schedule=schedule, **options)
        job.validate()
        self.jobs.append(job)
        return job

    def update(self, key: str, **changes: Any) -> Job:
        job = self.get(key)
        allowed = {"name", "command", "schedule", "enabled", "cwd", "catch_up",
                   "notify_failure", "timeout", "keep"}
        for name, value in changes.items():
            if name not in allowed:
                raise JobError(f"cannot change {name}")
            setattr(job, name, value)
        job.validate()
        return job

    def remove(self, key: str) -> Job:
        job = self.get(key)
        self.jobs.remove(job)
        return job


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def load(self) -> Jobs:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Jobs()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise JobError(f"{self.path} is not valid JSON ({error}); "
                           "it was left untouched") from None
        if not isinstance(data, dict):
            raise JobError(f"{self.path} is not a task file")
        return Jobs.from_dict(data)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Jobs]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            jobs = self.load()
            yield jobs
            self._write(jobs)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write(self, jobs: Jobs) -> None:
        jobs.revision += 1
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".tasks-", suffix=".tmp", delete=False)
        try:
            with handle:
                json.dump(jobs.to_dict(), handle, ensure_ascii=False, indent=1)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(handle.name, 0o600)
            backup = self.path.with_name(self.path.name + ".bak")
            with contextlib.suppress(OSError):
                os.unlink(backup)
            with contextlib.suppress(OSError):
                os.link(self.path, backup)
            os.replace(handle.name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise


def read_runs(paths: Paths, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """The most recent runs first."""
    path = paths.task_dir(job_id) / "runs.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append(record)
        if len(out) >= limit:
            break
    return out


def read_log(paths: Paths, job_id: str, name: str, limit: int = 256 * 1024) -> str:
    if "/" in name or name.startswith("."):
        return ""
    path = paths.task_dir(job_id) / "logs" / name
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except FileNotFoundError:
        return ""
    text = data.decode("utf-8", "replace")
    if size > limit:
        text = "…\n" + text.split("\n", 1)[-1]
    return text
