#!/usr/bin/env python3
"""kst-run: runs one kilix-scheduled-task task and records what happened.

systemd timers and crontab lines call this file, not the application, so a
schedule keeps working wherever the application is installed or moved to.
kilix-scheduled-task copies it to ``$XDG_DATA_HOME/kilix-scheduled-task/
kst-run.py``. It uses only the standard library and imports nothing from the
application.

    kst-run.py run --jobs TASKS.json --state STATE_DIR [--trigger manual] TASK_ID

For each run it writes the output to ``STATE_DIR/ID/logs/<start>.log``
(at most 1 MiB; the rest is counted, not kept) and appends one JSON line to
``STATE_DIR/ID/runs.jsonl``: start, end, duration, exit status, status
(ok, failed, timeout, stopped, skipped, error), trigger and log name. Only
the newest `keep` logs and the newest 500 records are kept. A run that finds
the previous one still going is recorded as skipped. The exit status is the
command's, so a failure shows in ``systemctl --user status``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_LOG = 1024 * 1024
MAX_RUNS = 500
STOP_GRACE = 10.0


def stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_job(jobs_path: Path, job_id: str) -> dict | None:
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for job in data.get("jobs", []) if isinstance(data, dict) else []:
        if isinstance(job, dict) and job.get("id") == job_id:
            return job
    return None


def environment(job: dict, trigger: str) -> dict[str, str]:
    env = dict(os.environ)
    home = env.get("HOME") or str(Path.home())
    env["HOME"] = home
    path = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for extra in (os.path.join(home, ".local", "bin"),):
        if os.path.isdir(extra) and extra not in path:
            path.insert(0, extra)
    for system in ("/usr/local/bin", "/usr/bin", "/bin"):
        if system not in path:
            path.append(system)
    env["PATH"] = os.pathsep.join(path)
    # cron starts jobs without the session bus; desktop notifications and
    # most desktop tools need it.
    bus = f"/run/user/{os.getuid()}/bus"
    if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(bus):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env["KST_TASK_ID"] = str(job.get("id", ""))
    env["KST_TASK_NAME"] = str(job.get("name", ""))
    env["KST_TRIGGER"] = trigger
    return env


def append_record(task_dir: Path, record: dict) -> None:
    path = task_dir / "runs.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(line)
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return
    if len(lines) > MAX_RUNS + 100:
        temp = path.with_name(".runs.tmp")
        temp.write_text("".join(lines[-MAX_RUNS:]), encoding="utf-8")
        os.replace(temp, path)


def prune_logs(log_dir: Path, keep: int) -> None:
    try:
        logs = sorted(p for p in log_dir.iterdir() if p.suffix == ".log")
    except OSError:
        return
    for old in logs[:-keep] if keep > 0 else logs:
        try:
            old.unlink()
        except OSError:
            pass


def notify(summary: str, body: str, env: dict[str, str]) -> None:
    for directory in env.get("PATH", "").split(os.pathsep):
        tool = os.path.join(directory, "notify-send")
        if os.access(tool, os.X_OK):
            try:
                subprocess.run([tool, "--app-name=kilix-scheduled-task",
                                "--urgency=normal", summary, body], env=env,
                               timeout=10, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
            return


class Run:
    def __init__(self, job: dict, task_dir: Path, trigger: str) -> None:
        self.job = job
        self.task_dir = task_dir
        self.trigger = trigger
        self.stop_requested = False
        self.child: subprocess.Popen | None = None

    def on_signal(self, number: int, _frame) -> None:
        self.stop_requested = True
        if self.child is not None and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except OSError:
                pass

    def execute(self) -> int:
        job = self.job
        log_dir = self.task_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        start = time.time()
        # The pid keeps two runs started in the same second apart.
        log_name = f"{file_stamp(start)}-{os.getpid()}.log"
        command = str(job.get("command", ""))
        cwd = os.path.expanduser(str(job.get("cwd") or "~"))
        env = environment(job, self.trigger)
        timeout = float(job.get("timeout") or 0)
        record = {"start": stamp(start), "trigger": self.trigger, "log": log_name}
        written = 0
        total = 0
        truncated = False
        status = "error"
        exit_code: int | None = None
        note = ""
        with open(log_dir / log_name, "wb") as log:
            header = (f"# {job.get('name', '')}\n# command: {command}\n"
                      f"# directory: {cwd}\n# started: {record['start']} ({self.trigger})\n\n")
            log.write(header.encode("utf-8", "replace"))
            log.flush()
            try:
                self.child = subprocess.Popen(
                    ["/bin/sh", "-c", command], cwd=cwd, env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as error:
                note = f"could not start: {error}"
                log.write(f"[kilix-scheduled-task: {note}]\n".encode())
                self.child = None
            if self.child is not None:
                stream = self.child.stdout
                assert stream is not None
                fd = stream.fileno()
                killed_at: float | None = None
                exited_at: float | None = None
                timed_out = False
                while True:
                    if exited_at is None and self.child.poll() is not None:
                        exited_at = time.time()
                    if exited_at is not None and time.time() - exited_at > 2.0:
                        # The command is done; a background process it left
                        # holding the pipe does not keep this run open.
                        break
                    if timeout and not timed_out and time.time() - start > timeout:
                        timed_out = True
                        killed_at = time.time()
                        try:
                            os.killpg(self.child.pid, signal.SIGTERM)
                        except OSError:
                            pass
                    if self.stop_requested and killed_at is None:
                        killed_at = time.time()
                    if killed_at is not None and time.time() - killed_at > STOP_GRACE:
                        try:
                            os.killpg(self.child.pid, signal.SIGKILL)
                        except OSError:
                            pass
                    try:
                        ready, _, _ = select.select([fd], [], [], 0.5)
                    except InterruptedError:
                        continue
                    if not ready:
                        if self.child.poll() is not None:
                            # The command exited but a background child may
                            # still hold the pipe open: stop waiting for it.
                            break
                        continue
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    room = MAX_LOG - written
                    if room > 0:
                        log.write(chunk[:room])
                        written += min(room, len(chunk))
                    if len(chunk) > room and not truncated:
                        truncated = True
                        log.write(b"\n[kilix-scheduled-task: output truncated at 1 MiB]\n")
                    log.flush()
                stream.close()
                exit_code = self.child.wait()
                if timed_out:
                    status, note = "timeout", f"stopped after {int(timeout)} s"
                elif self.stop_requested:
                    status, note = "stopped", "stopped by a signal"
                else:
                    status = "ok" if exit_code == 0 else "failed"
            end = time.time()
            log.write(f"\n# finished: {stamp(end)} status={status} exit={exit_code}\n".encode())
        record.update({"end": stamp(end), "duration": round(end - start, 3),
                       "exit": exit_code, "status": status, "bytes": total,
                       "truncated": truncated})
        if note:
            record["note"] = note
        append_record(self.task_dir, record)
        prune_logs(log_dir, int(job.get("keep") or 20))
        if status != "ok" and job.get("notify_failure"):
            notify(f"Scheduled task failed: {job.get('name', '')}",
                   f"{status}, exit {exit_code}" + (f" — {note}" if note else ""), env)
        if status == "ok":
            return 0
        return exit_code if isinstance(exit_code, int) and exit_code > 0 else 1


def run(args: argparse.Namespace) -> int:
    jobs_path = Path(args.jobs)
    state = Path(args.state)
    task_dir = state / args.id
    job = load_job(jobs_path, args.id)
    if job is None:
        print(f"kst-run: no task {args.id!r} in {jobs_path}; "
              "`kilix-scheduled-task sync` removes its schedule", file=sys.stderr)
        return 0
    task_dir.mkdir(parents=True, exist_ok=True)
    trigger = args.trigger
    marker = task_dir / "manual-request"
    if trigger == "schedule" and marker.exists():
        trigger = "manual"
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    schedule = job.get("schedule") or {}
    if trigger == "schedule":
        if not job.get("enabled", True):
            return 0
        if schedule.get("kind") == "once":
            # cron has no years: its line fires on that date every year.
            if str(schedule.get("at", ""))[:10] != datetime.now().date().isoformat():
                return 0
    lock_fd = os.open(task_dir / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            now = time.time()
            append_record(task_dir, {"start": stamp(now), "end": stamp(now), "duration": 0,
                                     "exit": None, "status": "skipped", "trigger": trigger,
                                     "note": "the previous run was still going"})
            return 0
        runner = Run(job, task_dir, trigger)
        previous = {n: signal.signal(n, runner.on_signal)
                    for n in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        running = task_dir / "running.json"
        running.write_text(json.dumps({"pid": os.getpid(), "start": stamp(time.time()),
                                       "trigger": trigger}), encoding="utf-8")
        try:
            return runner.execute()
        finally:
            try:
                running.unlink()
            except FileNotFoundError:
                pass
            for number, handler in previous.items():
                signal.signal(number, handler)
    finally:
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kst-run", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--jobs", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--trigger", default="schedule", choices=("schedule", "manual"))
    p.add_argument("id")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
