"""``kilix-scheduled-task``: the window with no arguments, a command line otherwise.

    kilix-scheduled-task add "Backup photos" --at "weekdays 02:00" -- rsync -a ~/Pictures /mnt/backup
    kilix-scheduled-task list
    kilix-scheduled-task next "every 15 minutes"     preview a schedule
    kilix-scheduled-task run backup-photos           now, as the schedule would
    kilix-scheduled-task logs backup-photos          the last run's output
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from typing import Sequence

from . import __version__
from .backends import SyncError, install_runner
from .jobs import JobError
from .schedule import ScheduleError, describe, parse
from .service import Scheduler, ago, duration, local_time, until, when

STATE_MARK = {"ok": "✓", "failed": "✗", "running": "…", "never": "·", "disabled": "-",
              "finished": "✓"}


class Out:
    def __init__(self) -> None:
        self.colour = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None \
            and os.environ.get("TERM") != "dumb"

    def paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.colour and text else text


def _report(report, out: Out) -> int:
    for name in report.changed:
        print(out.paint(f"  updated {name}", "2"))
    for name in report.removed:
        print(out.paint(f"  removed {name}", "2"))
    for error in report.errors:
        print(f"kilix-scheduled-task: {error}", file=sys.stderr)
    return 1 if report.errors else 0


def _command(args, words: list[str]) -> str | None:
    if getattr(args, "command_text", None):
        return args.command_text
    if not words:
        return None
    if len(words) == 1:
        return words[0]          # one word may be a whole shell command
    return shlex.join(words)


def _schedule_line(scheduler: Scheduler, job, now: datetime, out: Out) -> str:
    status = scheduler.status(job, now)
    mark = STATE_MARK[status.state]
    code = {"ok": "32", "failed": "31", "running": "33", "disabled": "2",
            "never": "2", "finished": "2"}[status.state]
    bits = [describe(job.schedule)]
    if status.next and job.enabled:
        bits.append(f"next {when(status.next, now)} ({until(status.next, now)})")
    if status.last and status.last.get("start"):
        started = local_time(status.last.get("start"))
        bits.append(f"last {status.last.get('status')} {ago(started, now)}")
    if not job.enabled:
        bits.append("disabled")
    return f"{out.paint(mark, code)} {job.id:<24} {out.paint(' · '.join(bits), '2')}"


# ---------------------------------------------------------------- commands


def cmd_add(args, words) -> int:
    scheduler = Scheduler()
    command = _command(args, words)
    if not command:
        raise JobError("give the command after --, e.g. ... --at 9am -- notify-send hi")
    schedule = parse(args.at)
    job, report = scheduler.add(args.name, command, schedule, enabled=not args.disabled,
                                cwd=args.dir or "~", catch_up=not args.no_catch_up,
                                notify_failure=args.notify_failure,
                                timeout=args.timeout or 0)
    out = Out()
    print(_schedule_line(scheduler, job, datetime.now(), out))
    print(out.paint(f"  runs: {job.command}", "2"))
    if scheduler.backend.name == "none":
        print("kilix-scheduled-task: saved, but nothing here can run it: "
              f"{scheduler.backend.available()[1]}", file=sys.stderr)
        return 1
    return _report(report, out)


def cmd_list(args, _words) -> int:
    scheduler = Scheduler()
    jobs = scheduler.load().jobs
    now = datetime.now()
    if args.json:
        rows = []
        for job in jobs:
            status = scheduler.status(job, now)
            rows.append({**job.to_dict(), "state": status.state,
                         "next": status.next.isoformat() if status.next else None,
                         "last": status.last})
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    out = Out()
    if not jobs:
        print(out.paint("no scheduled tasks (kilix-scheduled-task add --help)", "2"))
        return 0
    for job in jobs:
        print(_schedule_line(scheduler, job, now, out))
    return 0


def cmd_show(args, _words) -> int:
    scheduler = Scheduler()
    job = scheduler.load().get(args.task)
    now = datetime.now()
    status = scheduler.status(job, now)
    if args.json:
        print(json.dumps({**job.to_dict(), "state": status.state,
                          "next": [t.isoformat() for t in job.schedule.next(now, 5)],
                          "runs": scheduler.runs(job.id, 20)}, ensure_ascii=False, indent=1))
        return 0
    out = Out()
    print(out.paint(job.name, "1") + out.paint(f"  ({job.id})", "2"))
    rows = [("command", job.command), ("schedule", describe(job.schedule)),
            ("directory", job.cwd), ("state", status.state),
            ("next", ", ".join(when(t, now) for t in job.schedule.next(now, 5))),
            ("catch up", "yes" if job.catch_up else "no"),
            ("timeout", f"{job.timeout} s" if job.timeout else "none"),
            ("notify", "on failure" if job.notify_failure else "no"),
            ("backend", scheduler.backend.label)]
    for label, value in rows:
        if value:
            print(f"  {out.paint(label.ljust(10), '2')} {value}")
    runs = scheduler.runs(job.id, 10)
    if runs:
        print()
        for record in runs:
            print("  " + _run_line(record, now, out))
    return 0


def _run_line(record: dict, now: datetime, out: Out) -> str:
    status = record.get("status", "?")
    mark = {"ok": out.paint("✓", "32"), "failed": out.paint("✗", "31"),
            "timeout": out.paint("⏱", "31"), "stopped": out.paint("■", "33"),
            "skipped": out.paint("↷", "2"), "error": out.paint("!", "31")}.get(status, "?")
    started = local_time(record.get("start"))
    bits = [f"{when(started, now):<16}", f"{status:<8}", f"{duration(record.get('duration')):>6}"]
    if record.get("exit") not in (None, 0):
        bits.append(f"exit {record['exit']}")
    if record.get("trigger") == "manual":
        bits.append("manual")
    if record.get("note"):
        bits.append(record["note"])
    return f"{mark} " + "  ".join(bits)


def cmd_history(args, _words) -> int:
    scheduler = Scheduler()
    job = scheduler.load().get(args.task)
    now = datetime.now()
    out = Out()
    runs = scheduler.runs(job.id, args.n)
    if args.json:
        print(json.dumps(runs, ensure_ascii=False, indent=1))
        return 0
    if not runs:
        print(out.paint("no runs yet", "2"))
    for record in runs:
        print(_run_line(record, now, out))
    return 0


def cmd_logs(args, _words) -> int:
    scheduler = Scheduler()
    job = scheduler.load().get(args.task)
    runs = [r for r in scheduler.runs(job.id, 200) if r.get("log")]
    if not runs:
        print("no output recorded yet", file=sys.stderr)
        return 1
    if args.run >= len(runs):
        raise JobError(f"only {len(runs)} run(s) have output")
    sys.stdout.write(scheduler.log(job.id, runs[args.run]["log"]))
    return 0


def cmd_set_enabled(args, _words, enabled: bool) -> int:
    scheduler = Scheduler()
    out = Out()

    def change(jobs):
        return [jobs.update(key, enabled=enabled) for key in args.tasks]

    changed, report = scheduler.change(change)
    now = datetime.now()
    for job in changed:
        print(_schedule_line(scheduler, job, now, out))
    return _report(report, out)


def cmd_edit(args, words) -> int:
    scheduler = Scheduler()
    changes: dict = {}
    if args.name is not None:
        changes["name"] = args.name
    if args.at is not None:
        changes["schedule"] = parse(args.at)
    command = _command(args, words)
    if command is not None:
        changes["command"] = command
    if args.dir is not None:
        changes["cwd"] = args.dir
    if args.timeout is not None:
        changes["timeout"] = args.timeout
    if args.catch_up is not None:
        changes["catch_up"] = args.catch_up
    if args.notify_failure is not None:
        changes["notify_failure"] = args.notify_failure
    if not changes:
        raise JobError("nothing to change (see kilix-scheduled-task edit --help)")
    job, report = scheduler.update(args.task, **changes)
    out = Out()
    print(_schedule_line(scheduler, job, datetime.now(), out))
    return _report(report, out)


def cmd_rm(args, _words) -> int:
    scheduler = Scheduler()
    jobs = scheduler.load()
    targets = [jobs.get(key) for key in args.tasks]
    names = ", ".join(f"{j.name} ({j.id})" for j in targets)
    if not args.yes:
        if not sys.stdin.isatty():
            raise JobError("refusing without -y: standard input is not a terminal")
        answer = input(f"Remove {names} and its schedule"
                       f"{'' if args.keep_history else ' and run history'}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("nothing removed")
            return 1
    status = 0
    out = Out()
    for job in targets:
        _, report = scheduler.remove(job.id, keep_history=args.keep_history)
        status |= _report(report, out)
    print(f"removed {names}")
    return status


def cmd_run(args, _words) -> int:
    scheduler = Scheduler()
    job = scheduler.load().get(args.task)
    if args.here:
        install_runner(scheduler.paths)
        argv = scheduler.backend.runner_argv(job, "manual")
        code = subprocess.call(argv)
        runs = scheduler.runs(job.id, 1)
        if runs and runs[0].get("log"):
            sys.stdout.write(scheduler.log(job.id, runs[0]["log"]))
        return code
    scheduler.run_now(job.id)
    print(f"started {job.name}; `kilix-scheduled-task logs {job.id}` shows its output")
    return 0


def cmd_next(args, _words) -> int:
    schedule = parse(" ".join(args.when))
    now = datetime.now()
    out = Out()
    print(describe(schedule))
    if schedule.kind == "login":
        print(out.paint("  runs when your session starts", "2"))
    for moment in schedule.next(now, args.n):
        print(f"  {moment:%a %Y-%m-%d %H:%M}  {out.paint(until(moment, now), '2')}")
    if schedule.kind == "cron":
        print(out.paint(f"  cron: {schedule.cron}", "2"))
    return 0


def cmd_units(args, _words) -> int:
    scheduler = Scheduler()
    job = scheduler.load().get(args.task)
    text = scheduler.backend.preview(job)
    print(text or f"{scheduler.backend.label}: nothing to show")
    return 0


def cmd_sync(args, _words) -> int:
    scheduler = Scheduler()
    available, why = scheduler.backend.available()
    if not available:
        raise SyncError(f"{scheduler.backend.label}: {why}")
    report = scheduler.sync()
    out = Out()
    status = _report(report, out)
    if not report.changed and not report.removed and not report.errors:
        print("everything already matches")
    return status


def cmd_doctor(args, _words) -> int:
    scheduler = Scheduler()
    paths = scheduler.paths
    ok = True
    print(f"kilix-scheduled-task {__version__}")
    available, why = scheduler.backend.available()
    print(f"backend    {scheduler.backend.label}" + ("" if available else f" — unavailable: {why}"))
    ok &= available or scheduler.backend.name == "none"
    print(f"tasks      {paths.jobs}")
    print(f"history    {paths.state}")
    print(f"runner     {paths.runner}" + ("" if paths.runner.exists() else "  (installed at the first sync)"))
    if scheduler.backend.name == "systemd":
        print(f"units      {paths.units}")
    try:
        jobs = scheduler.load().jobs
    except JobError as error:
        print(f"           UNREADABLE: {error}")
        return 1
    for note in scheduler.backend.notes():
        print(f"note       {note}")
    now = datetime.now()
    for job in jobs:
        line = f"  {job.id:<24} {describe(job.schedule)}"
        if scheduler.backend.name == "systemd":
            state = scheduler.backend.timer_state(job)  # type: ignore[attr-defined]
            active = state.get("ActiveState", "?")
            enabled = state.get("UnitFileState", "?")
            expected = ("active", "enabled") if job.enabled else ("inactive", "disabled")
            if job.schedule.kind == "once" and not job.schedule.next(now):
                expected = (active, enabled)
            if (active, enabled) != expected:
                ok = False
                line += f"  MISMATCH timer {active}/{enabled} (run sync)"
            else:
                line += f"  timer {active}/{enabled}"
        print(line)
    try:
        from kilix_pane import deps
        for name, found, where in deps.report():
            print(f"{name:<22} {'ok ' if found else '-- '} {where}")
    except ImportError as error:
        print(f"window     unavailable: {error}")
    return 0 if ok else 1


def cmd_gui(args, _words) -> int:
    try:
        from kilix_pane import run
    except ImportError as error:
        raise JobError(f"the window needs kilix-pane: {error}") from None
    from .app import SchedulerApp

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise JobError("the window needs a terminal; try kilix-scheduled-task list")
    mode = "cells" if args.cells else "pixels" if args.pixels else "auto"
    run(SchedulerApp(Scheduler()), mode=mode, theme=args.theme, title="Scheduled tasks")
    return 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kilix-scheduled-task",
        description="Commands on a schedule: systemd user timers (or your crontab) "
                    "behind a window, a terminal UI and a command line.",
        epilog="Schedules: 08:30 · weekdays 8:30 · mon,wed,fri 18:00 · every 15 minutes · "
               "every 2 hours · monthly on the 1st at 9am · once tomorrow 9am · "
               "in 10 minutes · at login · or cron (*/5 9-17 * * 1-5).")
    parser.add_argument("--version", action="version", version=f"kilix-scheduled-task {__version__}")
    sub = parser.add_subparsers(dest="verb", metavar="COMMAND")

    def command(name, func, text, aliases=()):
        p = sub.add_parser(name, help=text, description=text, aliases=list(aliases))
        p.set_defaults(func=func)
        return p

    p = command("gui", cmd_gui, "open the window (the default with no command)", ["tui"])
    p.add_argument("--cells", action="store_true")
    p.add_argument("--pixels", action="store_true")
    p.add_argument("--theme", choices=("dark", "light"))

    p = command("add", cmd_add, "schedule a command: add NAME --at WHEN -- COMMAND…")
    p.add_argument("name")
    p.add_argument("--at", "-a", required=True, metavar="WHEN")
    p.add_argument("--command", "-c", dest="command_text", metavar="SHELL",
                   help="the command as one shell string (instead of words after --)")
    p.add_argument("--dir", "-d", help="working directory (default ~)")
    p.add_argument("--timeout", type=int, help="stop it after this many seconds")
    p.add_argument("--no-catch-up", action="store_true",
                   help="skip runs missed while the computer was off")
    p.add_argument("--notify-failure", action="store_true",
                   help="a desktop notification when it fails")
    p.add_argument("--disabled", action="store_true", help="save it without scheduling it")

    p = command("list", cmd_list, "every task with its next and last run", ["ls"])
    p.add_argument("--json", action="store_true")

    p = command("show", cmd_show, "one task in full, with recent runs")
    p.add_argument("task")
    p.add_argument("--json", action="store_true")

    p = command("history", cmd_history, "a task's runs, newest first")
    p.add_argument("task")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--json", action="store_true")

    p = command("logs", cmd_logs, "a run's output (the newest by default)", ["log"])
    p.add_argument("task")
    p.add_argument("--run", type=int, default=0, help="0 is the newest, 1 the one before…")

    p = command("enable", lambda a, w: cmd_set_enabled(a, w, True), "schedule tasks again")
    p.add_argument("tasks", nargs="+")
    p = command("disable", lambda a, w: cmd_set_enabled(a, w, False), "pause tasks")
    p.add_argument("tasks", nargs="+")

    p = command("edit", cmd_edit, "change a task: edit TASK [--at …] [-- COMMAND…]")
    p.add_argument("task")
    p.add_argument("--name")
    p.add_argument("--at", "-a", metavar="WHEN")
    p.add_argument("--command", "-c", dest="command_text", metavar="SHELL")
    p.add_argument("--dir", "-d")
    p.add_argument("--timeout", type=int)
    p.add_argument("--catch-up", dest="catch_up", action="store_true", default=None)
    p.add_argument("--no-catch-up", dest="catch_up", action="store_false")
    p.add_argument("--notify-failure", dest="notify_failure", action="store_true", default=None)
    p.add_argument("--no-notify-failure", dest="notify_failure", action="store_false")

    p = command("rm", cmd_rm, "remove tasks and their schedules", ["remove"])
    p.add_argument("tasks", nargs="+")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--keep-history", action="store_true")

    p = command("run", cmd_run, "run a task now, the way its schedule would")
    p.add_argument("task")
    p.add_argument("--here", action="store_true",
                   help="in this terminal, waiting for it, then print its output")

    p = command("next", cmd_next, "preview a schedule: its next runs")
    p.add_argument("when", nargs="+")
    p.add_argument("-n", type=int, default=5)

    p = command("units", cmd_units, "the systemd units or crontab line a task becomes")
    p.add_argument("task")

    command("sync", cmd_sync, "make systemd or the crontab match the task list again")
    command("doctor", cmd_doctor, "check the backend, the runner and every task's timer")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    words: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        argv, words = argv[:index], argv[index + 1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verb is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        args = parser.parse_args([*argv, "gui" if interactive else "list"])
    try:
        return args.func(args, words)
    except (JobError, ScheduleError, SyncError) as error:
        print(f"kilix-scheduled-task: {error}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
