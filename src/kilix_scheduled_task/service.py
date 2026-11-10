"""The operations the CLI and the window share: change the definitions, then
make the system match them."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from .backends import Backend, Report, SyncError, choose, default_run
from .jobs import Job, JobError, Jobs, Paths, Store, read_log, read_runs

__all__ = ["Scheduler", "Status", "ago", "until", "local_time"]


@dataclass
class Status:
    job: Job
    next: datetime | None
    last: dict[str, Any] | None

    @property
    def state(self) -> str:
        """ok, failed, running, never, disabled or finished."""
        if not self.job.enabled:
            return "disabled"
        if self.last and self.last.get("status") == "running":
            return "running"
        if self.job.schedule.kind == "once" and self.next is None:
            return "finished"
        if self.last is None:
            return "never"
        return "ok" if self.last.get("status") in ("ok", "skipped") else "failed"


class Scheduler:
    def __init__(self, paths: Paths | None = None, backend: Backend | None = None,
                 run=default_run) -> None:
        self.paths = paths or Paths.default()
        self.store = Store(self.paths.jobs)
        self.backend = backend or choose(self.paths, run)

    # ---------------------------------------------------------------- read

    def load(self) -> Jobs:
        return self.store.load()

    def status(self, job: Job, now: datetime | None = None) -> Status:
        now = now or datetime.now()
        upcoming = job.schedule.next(now, 1)
        runs = read_runs(self.paths, job.id, 1)
        last = runs[0] if runs else None
        if self.running(job):
            last = {"status": "running", "start": last.get("start") if last else None}
        return Status(job, upcoming[0] if upcoming else None, last)

    def running(self, job: Job) -> bool:
        """True while a run is going: the runner leaves ``running.json`` with
        its pid. (Probing the run lock instead would make a run that starts
        at that moment think the previous one is still going.)"""
        path = self.paths.task_dir(job.id) / "running.json"
        try:
            pid = int(json.loads(path.read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def runs(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return read_runs(self.paths, job_id, limit)

    def log(self, job_id: str, name: str) -> str:
        return read_log(self.paths, job_id, name)

    # -------------------------------------------------------------- change

    @contextlib.contextmanager
    def _sync_lock(self) -> Iterator[None]:
        self.paths.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.paths.state / "sync.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def sync(self, jobs: Jobs | None = None) -> Report:
        with self._sync_lock():
            current = jobs if jobs is not None else self.load()
            return self.backend.sync(current.jobs)

    def change(self, edit: Callable[[Jobs], Any]) -> tuple[Any, Report]:
        """Apply `edit` to the definitions, save, then sync. A sync failure
        leaves the definitions saved and is reported, not raised."""
        with self.store.transaction() as jobs:
            result = edit(jobs)
        try:
            report = self.sync(jobs)
        except (OSError, SyncError) as error:
            report = Report(errors=[str(error)])
        return result, report

    def add(self, name: str, command: str, schedule, **options: Any) -> tuple[Job, Report]:
        return self.change(lambda jobs: jobs.add(name, command, schedule, **options))

    def update(self, key: str, **changes: Any) -> tuple[Job, Report]:
        return self.change(lambda jobs: jobs.update(key, **changes))

    def remove(self, key: str, keep_history: bool = False) -> tuple[Job, Report]:
        job, report = self.change(lambda jobs: jobs.remove(key))
        if not keep_history:
            directory = self.paths.task_dir(job.id)
            if directory.is_dir() and directory.parent == self.paths.state:
                shutil.rmtree(directory, ignore_errors=True)
        return job, report

    def run_now(self, key: str) -> Job:
        job = self.load().get(key)
        self.backend.run_now(job)
        return job


# ------------------------------------------------------------- formatting


def local_time(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return moment.astimezone().replace(tzinfo=None)


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours and days < 7 else f"{days}d"


def until(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return ""
    delta = (moment - now).total_seconds()
    return "now" if delta < 1 else "in " + _span(delta)


def ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return ""
    delta = (now - moment).total_seconds()
    return "just now" if delta < 5 else _span(delta) + " ago"


def when(moment: datetime | None, now: datetime) -> str:
    """``14:05``, ``tomorrow 02:00``, ``Fri 02:00``, ``2026-12-24 18:00``."""
    if moment is None:
        return ""
    days = (moment.date() - now.date()).days
    clock = moment.strftime("%H:%M")
    if days == 0:
        return clock
    if days == 1:
        return "tomorrow " + clock
    if days == -1:
        return "yesterday " + clock
    if -7 < days < 7:
        return moment.strftime("%a ") + clock
    return moment.strftime("%Y-%m-%d ") + clock


def duration(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return ""
    if value < 10:
        return f"{value:.1f}s"
    return _span(value)


def next_runs_text(job: Job, now: datetime, count: int = 3) -> str:
    runs = job.schedule.next(now, count)
    return ", ".join(when(t, now) for t in runs)


__all__ += ["when", "duration", "next_runs_text", "JobError", "SyncError"]
