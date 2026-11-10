"""Turning task definitions into real schedules: systemd user timers, or the
user's crontab where there is no systemd user session.

Both backends are *synced*, never patched: `sync(jobs)` makes the system match
the definitions, writing what is missing or different and removing what no
longer has a definition. It only ever touches what it generated: unit files
that carry the ``X-KilixScheduledTask=`` marker, and the lines between this
program's markers in the crontab. Everything else is left exactly as it was.

Every external command goes through ``self.run``, so the tests drive the
backends with a fake systemctl and crontab.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .jobs import Job, Paths
from .schedule import to_crontab, to_oncalendar

__all__ = ["Backend", "CronBackend", "NullBackend", "SystemdBackend", "SyncError",
           "choose", "install_runner"]

MARKER = "X-KilixScheduledTask"
CRON_BEGIN = "# BEGIN kilix-scheduled-task: managed, change with kilix-scheduled-task"
CRON_END = "# END kilix-scheduled-task"
DEFAULT_PREFIX = "kilix-task-"


class SyncError(RuntimeError):
    pass


Runner = Callable[[Sequence[str], str | None], subprocess.CompletedProcess]


def default_run(argv: Sequence[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(argv), input=stdin, capture_output=True, text=True,
                              timeout=60)
    except FileNotFoundError as error:
        return subprocess.CompletedProcess(list(argv), 127, "", str(error))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", "timed out")


def python() -> str:
    """The interpreter the schedules use: the system's, which outlives venvs."""
    return "/usr/bin/python3" if os.access("/usr/bin/python3", os.X_OK) else sys.executable


def install_runner(paths: Paths) -> bool:
    """Copy the runner into place if it is missing or different."""
    source = Path(__file__).with_name("runner.py").read_bytes()
    target = paths.runner
    try:
        if target.read_bytes() == source:
            return False
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(".kst-run.tmp")
    temp.write_bytes(source)
    os.chmod(temp, 0o755)
    os.replace(temp, target)
    return True


@dataclass
class Report:
    """What a sync did, for the user."""

    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def raise_errors(self) -> None:
        if self.errors:
            raise SyncError("; ".join(self.errors))


class Backend:
    name = "none"
    label = "not scheduled"

    def __init__(self, paths: Paths, run: Runner = default_run,
                 prefix: str | None = None) -> None:
        self.paths = paths
        self.run = run
        self.prefix = prefix or os.environ.get("KILIX_SCHEDULED_TASK_PREFIX") or DEFAULT_PREFIX

    def available(self) -> tuple[bool, str]:
        return False, "no backend"

    def sync(self, jobs: Sequence[Job]) -> Report:
        return Report()

    def run_now(self, job: Job) -> None:
        raise SyncError("nothing can run tasks here: no systemd user session and no crontab")

    def runner_argv(self, job: Job, trigger: str = "schedule") -> list[str]:
        argv = [python(), str(self.paths.runner), "run", "--jobs", str(self.paths.jobs),
                "--state", str(self.paths.state)]
        if trigger != "schedule":
            argv += ["--trigger", trigger]
        return argv + [job.id]

    def request_manual(self, job: Job) -> None:
        directory = self.paths.task_dir(job.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manual-request").touch()

    def spawn(self, job: Job) -> None:
        install_runner(self.paths)
        subprocess.Popen(self.runner_argv(job, "manual"), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(Path.home()))

    def preview(self, job: Job) -> str:
        return ""

    def notes(self) -> list[str]:
        return []


class NullBackend(Backend):
    def available(self) -> tuple[bool, str]:
        return False, "no systemd user session and no crontab command"


# ------------------------------------------------------------------ systemd


def systemd_quote(word: str) -> str:
    """One ExecStart= argument: specifiers and variables escaped, quoted if
    it has spaces or quotes."""
    word = word.replace("%", "%%").replace("$", "$$")
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", word):
        return word
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _one_line(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class SystemdBackend(Backend):
    name = "systemd"
    label = "systemd user timers"

    def unit(self, job_id: str, kind: str) -> str:
        return f"{self.prefix}{job_id}.{kind}"

    def systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return self.run(["systemctl", "--user", *args], None)

    def available(self) -> tuple[bool, str]:
        if shutil.which("systemctl") is None and self.run is default_run:
            return False, "systemctl is not installed"
        result = self.systemctl("show-environment")
        if result.returncode != 0:
            return False, (result.stderr.strip() or "no systemd user session")
        return True, ""

    def service_text(self, job: Job) -> str:
        argv = " ".join(systemd_quote(a) for a in self.runner_argv(job))
        return "\n".join([
            "# Generated by kilix-scheduled-task. Change the task with",
            "# kilix-scheduled-task; edits here are replaced at the next change.",
            f"# Runs: {_one_line(job.command)}",
            "[Unit]",
            f"Description=kilix-scheduled-task: {_one_line(job.name, 80)}",
            f"{MARKER}={job.id}",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={argv}",
            "",
        ])

    def timer_text(self, job: Job) -> str:
        lines = [
            "# Generated by kilix-scheduled-task. Change the task with",
            "# kilix-scheduled-task; edits here are replaced at the next change.",
            "[Unit]",
            f"Description=kilix-scheduled-task timer: {_one_line(job.name, 80)}",
            f"{MARKER}={job.id}",
            "",
            "[Timer]",
        ]
        if job.schedule.kind == "login":
            lines.append("OnStartupSec=30s")
        else:
            lines += [f"OnCalendar={value}" for value in to_oncalendar(job.schedule)]
            if job.catch_up:
                lines.append("Persistent=true")
        lines += ["AccuracySec=1s", f"Unit={self.unit(job.id, 'service')}", "",
                  "[Install]", "WantedBy=timers.target", ""]
        return "\n".join(lines)

    def preview(self, job: Job) -> str:
        return (f"# {self.paths.units / self.unit(job.id, 'timer')}\n{self.timer_text(job)}\n"
                f"# {self.paths.units / self.unit(job.id, 'service')}\n{self.service_text(job)}")

    def ours(self) -> dict[str, Path]:
        """Unit files this program generated, by file name."""
        out: dict[str, Path] = {}
        try:
            entries = list(self.paths.units.iterdir())
        except FileNotFoundError:
            return out
        for path in entries:
            if not path.name.startswith(self.prefix) or path.suffix not in (".service", ".timer"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f"\n{MARKER}=" in text:
                out[path.name] = path
        return out

    def sync(self, jobs: Sequence[Job]) -> Report:
        report = Report()
        install_runner(self.paths)
        self.paths.units.mkdir(parents=True, exist_ok=True)
        wanted: dict[str, str] = {}
        for job in jobs:
            wanted[self.unit(job.id, "service")] = self.service_text(job)
            wanted[self.unit(job.id, "timer")] = self.timer_text(job)
        existing = self.ours()
        changed_timers: set[str] = set()
        for name, text in wanted.items():
            path = self.paths.units / name
            try:
                current = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                current = None
            if current == text:
                continue
            if current is not None and name not in existing:
                report.errors.append(f"{path} exists and was not made by "
                                     "kilix-scheduled-task; left alone")
                continue
            temp = path.with_name(f".{name}.tmp")
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, path)
            report.changed.append(name)
            if name.endswith(".timer"):
                changed_timers.add(name)
        stale = [name for name in existing if name not in wanted]
        for name in sorted(stale):
            if name.endswith(".timer"):
                result = self.systemctl("disable", "--now", name)
                if result.returncode != 0 and "not loaded" not in result.stderr \
                        and "does not exist" not in result.stderr:
                    report.errors.append(f"disable {name}: {result.stderr.strip()}")
        for name in sorted(stale):
            try:
                existing[name].unlink()
                report.removed.append(name)
            except OSError as error:
                report.errors.append(f"remove {name}: {error}")
        if report.changed or report.removed:
            result = self.systemctl("daemon-reload")
            if result.returncode != 0:
                report.errors.append(f"daemon-reload: {result.stderr.strip()}")
        for job in jobs:
            timer = self.unit(job.id, "timer")
            if timer not in wanted or (self.paths.units / timer).read_text(
                    encoding="utf-8") != wanted[timer]:
                continue   # a foreign file we refused to replace
            if job.enabled:
                result = self.systemctl("enable", "--now", timer)
                if result.returncode == 0 and timer in changed_timers:
                    # A running timer keeps its old schedule until restarted.
                    result = self.systemctl("restart", timer)
            else:
                result = self.systemctl("disable", "--now", timer)
            if result.returncode != 0:
                report.errors.append(f"{timer}: {result.stderr.strip() or 'failed'}")
        return report

    def run_now(self, job: Job) -> None:
        install_runner(self.paths)
        self.request_manual(job)
        result = self.systemctl("start", "--no-block", self.unit(job.id, "service"))
        if result.returncode != 0:
            raise SyncError(result.stderr.strip() or "systemctl start failed")

    def timer_state(self, job: Job) -> dict[str, str]:
        result = self.systemctl("show", self.unit(job.id, "timer"), "--property",
                                "ActiveState,UnitFileState,NextElapseUSecRealtime,LastTriggerUSec")
        out = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            out[key] = value
        return out

    def notes(self) -> list[str]:
        user = os.environ.get("USER") or ""
        if not user:
            return []
        result = self.run(["loginctl", "show-user", user, "--property=Linger", "--value"], None)
        if result.returncode == 0 and result.stdout.strip() == "no":
            return ["Timers run only while you are logged in. "
                    "`loginctl enable-linger` keeps them running after you log out."]
        return []


# --------------------------------------------------------------------- cron


def cron_quote(word: str) -> str:
    # % ends the command in a crontab line unless escaped.
    return shlex.quote(word).replace("%", "\\%")


class CronBackend(Backend):
    name = "cron"
    label = "your crontab"

    def available(self) -> tuple[bool, str]:
        if shutil.which("crontab") is None and self.run is default_run:
            return False, "crontab is not installed"
        return True, ""

    def line(self, job: Job) -> str:
        command = " ".join(cron_quote(a) for a in self.runner_argv(job))
        entry = f"{to_crontab(job.schedule)} {command} # {self.prefix}{job.id}"
        return entry if job.enabled else "# (disabled) " + entry

    def preview(self, job: Job) -> str:
        return self.line(job)

    def read(self) -> str:
        result = self.run(["crontab", "-l"], None)
        if result.returncode != 0:
            if "no crontab" in (result.stderr or "").lower():
                return ""
            raise SyncError(f"crontab -l: {result.stderr.strip()}")
        return result.stdout

    def sync(self, jobs: Sequence[Job]) -> Report:
        report = Report()
        install_runner(self.paths)
        try:
            current = self.read()
        except SyncError as error:
            report.errors.append(str(error))
            return report
        lines = current.splitlines()
        outside: list[str] = []
        inside = False
        position: int | None = None
        for line in lines:
            if line.strip() == CRON_BEGIN:
                inside = True
                position = len(outside) if position is None else position
                continue
            if line.strip() == CRON_END:
                inside = False
                continue
            if not inside:
                outside.append(line)
        if inside:
            report.errors.append("the crontab has a BEGIN kilix-scheduled-task line "
                                 "without its END line; fix it by hand (crontab -e)")
            return report
        block = [CRON_BEGIN, *(self.line(job) for job in jobs), CRON_END] if jobs else []
        if position is None:
            while outside and not outside[-1].strip():
                outside.pop()
            new_lines = outside + ([""] if outside and block else []) + block
        else:
            new_lines = outside[:position] + block + outside[position:]
        text = "\n".join(new_lines) + ("\n" if new_lines else "")
        if text == current or (not text.strip() and not current.strip()):
            return report
        result = self.run(["crontab", "-"], text)
        if result.returncode != 0:
            report.errors.append(f"crontab: {result.stderr.strip()}")
        else:
            report.changed.append("crontab")
        return report

    def run_now(self, job: Job) -> None:
        self.request_manual(job)
        self.spawn(job)

    def notes(self) -> list[str]:
        found = any(self.run(["pgrep", "-c", "-x", name], None).returncode == 0
                    for name in ("cron", "crond", "cronie"))
        if not found:
            return ["No cron daemon seems to be running; the crontab lines are "
                    "there but nothing will run them."]
        return []


def choose(paths: Paths, run: Runner = default_run, name: str | None = None) -> Backend:
    """``$KILIX_SCHEDULED_TASK_BACKEND`` (systemd, cron, none), else systemd
    when there is a user session, else cron when there is a crontab."""
    wanted = name or os.environ.get("KILIX_SCHEDULED_TASK_BACKEND")
    classes = {"systemd": SystemdBackend, "cron": CronBackend, "none": NullBackend}
    if wanted:
        if wanted not in classes:
            raise SyncError(f"unknown backend {wanted!r} (systemd, cron or none)")
        return classes[wanted](paths, run)
    for cls in (SystemdBackend, CronBackend):
        backend = cls(paths, run)
        if backend.available()[0]:
            return backend
    return NullBackend(paths, run)
