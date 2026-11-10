"""The kilix-scheduled-task window: tasks on the left, the selected task's
schedule, history and output on the right.

Nothing here talks to systemd or cron directly; every change goes through
`Scheduler`, which saves the definitions and then syncs the backend, exactly
as the command line does.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from typing import Any, Callable

from kilix_pane import (Button, ButtonRow, ConfirmDialog, Field, FormDialog,
                        HelpOverlay, ListView, Paste, Rect, chord, is_click,
                        mouse, text_width, wrap)
from kilix_pane import deps

from .backends import SyncError
from .jobs import Job, JobError
from .schedule import ScheduleError, describe, parse
from .service import (Scheduler, Status, ago, duration, local_time, until,
                      when)

STATE_ROLE = {"ok": "ok", "failed": "danger", "running": "warn", "never": "ink_faint",
              "disabled": "ink_faint", "finished": "ink_faint"}
STATE_WORD = {"ok": "ok", "failed": "failed", "running": "running", "never": "not run yet",
              "disabled": "paused", "finished": "finished"}
RUN_MARK = {"ok": ("✓", "ok"), "failed": ("✗", "danger"), "timeout": ("⏱", "danger"),
            "stopped": ("■", "warn"), "skipped": ("↷", "ink_faint"), "error": ("!", "danger")}
MESSAGE_SECONDS = 5.0

HELP = [
    ("n  a", "new task"),
    ("e  enter", "edit the task"),
    ("space", "pause / resume"),
    ("r", "run now (asks first)"),
    ("d  delete", "delete (asks first)"),
    ("tab  l  h", "between tasks and their runs"),
    ("enter (runs)", "open that run's output in $PAGER"),
    ("u", "the systemd units or crontab line"),
    ("y", "copy the command"),
    ("s", "sync the backend again"),
    ("?  q", "help, quit"),
    ("", ""),
    ("schedules", "08:30 · weekdays 8:30 · mon,fri 18:00"),
    ("", "every 15 minutes · every 2 hours · sun 03:00"),
    ("", "monthly on the 1st at 9am · once tomorrow 9am"),
    ("", "in 10 minutes · at login · */5 9-17 * * 1-5"),
]


def _key(event: Any) -> str | None:
    key = chord(event)
    if key and key.startswith("shift+") and len(key) == 7 and key[6].isalpha():
        return key[6].upper()
    return key


class TaskForm(FormDialog):
    """The task form, with the schedule read back as you type it."""

    def __init__(self, title: str, fields: list[Field], now: Callable[[], datetime],
                 **kwargs: Any) -> None:
        super().__init__(title, fields, **kwargs)
        self.now = now

    def render(self, painter: Any, area: Rect) -> None:
        for f in self.fields:
            if f.key == "when":
                text = self.edits["when"].text
                if not text.strip():
                    f.hint = "weekdays 8:30 · every 15 minutes · once tomorrow 9am"
                    continue
                try:
                    schedule = parse(text, self.now())
                    upcoming = schedule.next(self.now(), 3)
                    shown = ", ".join(when(t, self.now()) for t in upcoming)
                    f.hint = f"↳ {describe(schedule)}" + (f" — next {shown}" if shown else "")
                except ScheduleError as error:
                    f.hint = f"✗ {error}"
        super().render(painter, area)


class TextView:
    """A scrollable block of text (a run's output)."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.top = 0
        self.rect = Rect(0, 0, 0, 0)
        self.follow = True

    def set(self, text: str) -> None:
        self.lines = text.replace("\t", "    ").splitlines()
        if self.follow:
            self.top = max(0, len(self.lines) - max(1, self.rect.h))

    def scroll(self, step: int) -> None:
        self.top = max(0, min(self.top + step, max(0, len(self.lines) - self.rect.h)))
        self.follow = self.top >= len(self.lines) - self.rect.h

    def render(self, p: Any, rect: Rect) -> None:
        self.rect = rect
        if self.follow:
            self.top = max(0, len(self.lines) - rect.h)
        for row in range(rect.h):
            index = self.top + row
            if index >= len(self.lines):
                break
            line = "".join(c if c.isprintable() else "·" for c in self.lines[index])
            role = "ink_faint" if line.startswith("# ") else "ink_muted"
            p.text(rect.x, rect.y + row, line, role, limit=rect.right)


class SchedulerApp:
    def __init__(self, scheduler: Scheduler | None = None, *,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self.scheduler = scheduler or Scheduler()
        self.now = now
        self.jobs: list[Job] = []
        self.statuses: dict[str, Status] = {}
        self.error = ""
        self.sync_error = ""
        self.stamp: Any = None
        self.list = ListView(row_height=2)
        self.runs_list = ListView(row_height=1)
        self.runs: list[dict[str, Any]] = []
        self.runs_stamp: Any = None
        self.output = TextView()
        self.focus = "jobs"
        self.modal: Any = None
        self.on_modal: Callable[[str, Any], None] | None = None
        self.message = ""
        self.message_role = "ink_muted"
        self.message_until = 0.0
        self.toolbar = ButtonRow([Button("New", "new", "n", primary=True),
                                  Button("Run now", "run", "r"),
                                  Button("Help", "help", "?")])
        available, why = self.scheduler.backend.available()
        self.backend_ok = available
        self.backend_why = why
        self.notes = self.scheduler.backend.notes() if available else []
        self.reload()

    # ---------------------------------------------------------------- data

    def reload(self) -> None:
        selected = self.selected.id if self.selected else None
        try:
            self.jobs = self.scheduler.load().jobs
            self.error = ""
        except (JobError, OSError) as error:
            self.error = str(error)
        self.stamp = self.scheduler.store.stamp()
        self.refresh_status()
        self.list.set_count(len(self.jobs))
        ids = [j.id for j in self.jobs]
        if selected in ids:
            self.list.select(ids.index(selected))
        self.load_runs(force=True)

    def refresh_status(self) -> None:
        now = self.now()
        self.statuses = {job.id: self.scheduler.status(job, now) for job in self.jobs}

    @property
    def selected(self) -> Job | None:
        if 0 <= self.list.selected < len(self.jobs):
            return self.jobs[self.list.selected]
        return None

    def runs_file_stamp(self) -> Any:
        job = self.selected
        if job is None:
            return None
        path = self.scheduler.paths.task_dir(job.id) / "runs.jsonl"
        running = self.scheduler.paths.task_dir(job.id) / "running.json"
        stamps = []
        for item in (path, running):
            try:
                st = item.stat()
                stamps.append((st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                stamps.append(None)
        return (job.id, *stamps)

    def load_runs(self, force: bool = False) -> None:
        stamp = self.runs_file_stamp()
        if not force and stamp == self.runs_stamp:
            return
        self.runs_stamp = stamp
        job = self.selected
        self.runs = self.scheduler.runs(job.id, 100) if job else []
        self.runs_list.set_count(len(self.runs))
        if force:
            self.runs_list.select(0)
            self.output.follow = True
        self.load_output()

    def load_output(self) -> None:
        job = self.selected
        record = self.selected_run
        if job is None or record is None or not record.get("log"):
            self.output.set("")
            return
        self.output.set(self.scheduler.log(job.id, record["log"]))

    @property
    def selected_run(self) -> dict[str, Any] | None:
        if 0 <= self.runs_list.selected < len(self.runs):
            return self.runs[self.runs_list.selected]
        return None

    def flash(self, message: str, role: str = "ink_muted") -> None:
        import time
        self.message, self.message_role = message, role
        self.message_until = time.monotonic() + MESSAGE_SECONDS

    def apply(self, action: Callable[[], Any], message: str) -> Any:
        """Run a Scheduler change, report the outcome, reload."""
        try:
            result = action()
        except (JobError, ScheduleError, SyncError, OSError) as error:
            self.flash(str(error), "danger")
            self.reload()
            return None
        report = result[1] if isinstance(result, tuple) and len(result) == 2 else None
        if report is not None and report.errors:
            self.sync_error = "; ".join(report.errors)
            self.flash(f"{message} — but the backend said: {self.sync_error}", "danger")
        else:
            if report is not None:
                self.sync_error = ""
            self.flash(message, "ok")
        self.reload()
        return result

    # ------------------------------------------------------------- dialogs

    def open_modal(self, dialog: Any, then: Callable[[str, Any], None] | None) -> None:
        self.modal, self.on_modal = dialog, then

    def task_form(self, job: Job | None) -> None:
        fields = [
            Field("name", "Name", job.name if job else "", placeholder="Backup photos"),
            Field("when", "When", self.schedule_text(job) if job else ""),
            Field("command", "Command", job.command if job else "",
                  placeholder="rsync -a ~/Pictures /mnt/backup",
                  hint="run by /bin/sh in the directory below"),
            Field("cwd", "Directory", job.cwd if job else "~"),
            Field("timeout", "Time limit", str(job.timeout) if job and job.timeout else "",
                  placeholder="none", hint="seconds; empty for none"),
            Field("catch_up", "Catch up", "yes" if (job is None or job.catch_up) else "no",
                  kind="choice", choices=["yes", "no"],
                  hint="run once at start-up if the computer was off at the time"),
            Field("notify", "Notify", "on failure" if job and job.notify_failure else "no",
                  kind="choice", choices=["no", "on failure"]),
        ]

        def parse_values(values: dict[str, str]) -> dict[str, Any]:
            if not values["name"]:
                raise JobError("a task needs a name")
            if not values["command"]:
                raise JobError("a task needs a command")
            timeout = values["timeout"].strip()
            if timeout and not timeout.isdigit():
                raise JobError("the time limit is a number of seconds")
            return {"name": values["name"], "schedule": parse(values["when"], self.now()),
                    "command": values["command"], "cwd": values["cwd"] or "~",
                    "timeout": int(timeout or 0), "catch_up": values["catch_up"] == "yes",
                    "notify_failure": values["notify"] == "on failure"}

        def validate(values: dict[str, str]) -> str | None:
            try:
                parse_values(values)
            except (JobError, ScheduleError) as error:
                return str(error)
            return None

        def done(result: str, dialog: TaskForm) -> None:
            if result != "submit":
                return
            values = parse_values(dialog.values())
            if job is not None:
                self.apply(lambda: self.scheduler.update(job.id, **values), "Saved")
                return
            self.confirm_new(values)

        self.open_modal(TaskForm("Edit task" if job else "New task", fields, self.now,
                                 validate=validate, width=84), done)

    def schedule_text(self, job: Job) -> str:
        """Text that parses back to this schedule."""
        s = job.schedule
        if s.kind == "login":
            return "at login"
        if s.kind == "once":
            return "once " + s.at.replace("T", " ")
        return s.cron

    def confirm_new(self, values: dict[str, Any]) -> None:
        schedule = values["schedule"]
        upcoming = schedule.next(self.now(), 2)
        nxt = ", ".join(when(t, self.now()) for t in upcoming)
        lines = [f"Runs: {values['command']}", f"In: {values['cwd']}",
                 f"When: {describe(schedule)}" + (f" (next {nxt})" if nxt else ""),
                 f"By: {self.scheduler.backend.label}"]
        if not self.backend_ok:
            lines.append(f"Nothing here can run it yet: {self.backend_why}")

        def done(result: str, _dialog: Any) -> None:
            if result != "confirm":
                return
            name = values.pop("name")
            command = values.pop("command")
            schedule_value = values.pop("schedule")
            outcome = self.apply(lambda: self.scheduler.add(name, command, schedule_value, **values),
                                 f"Scheduled “{name}”")
            if outcome:
                ids = [j.id for j in self.jobs]
                if outcome[0].id in ids:
                    self.list.select(ids.index(outcome[0].id))
                    self.load_runs(force=True)

        self.open_modal(ConfirmDialog(f"Schedule “{values['name']}”?", "\n".join(lines),
                                      "Schedule"), done)

    def confirm_run(self, job: Job) -> None:
        def done(result: str, _dialog: Any) -> None:
            if result == "confirm":
                self.apply(lambda: self.scheduler.run_now(job.id), f"Started “{job.name}”")
                self.focus = "runs"

        self.open_modal(ConfirmDialog(f"Run “{job.name}” now?", job.command, "Run"), done)

    def confirm_delete(self, job: Job) -> None:
        def done(result: str, _dialog: Any) -> None:
            if result == "confirm":
                self.apply(lambda: self.scheduler.remove(job.id), f"Deleted “{job.name}”")

        self.open_modal(ConfirmDialog(
            f"Delete “{job.name}”?",
            f"Its schedule ({describe(job.schedule)}) and its run history go too.",
            "Delete", danger=True), done)

    def show_units(self, job: Job) -> None:
        text = self.scheduler.backend.preview(job) or "Nothing is scheduled here."
        rows = [(line, "") for line in text.splitlines()[:40]]
        self.open_modal(HelpOverlay(self.scheduler.backend.label, rows), None)

    def open_pager(self, ctx: Any) -> None:
        job, record = self.selected, self.selected_run
        if job is None or record is None or not record.get("log"):
            return
        path = self.scheduler.paths.task_dir(job.id) / "logs" / record["log"]
        pager = os.environ.get("PAGER") or ("less" if shutil.which("less") else "more")
        argv = [*pager.split(), str(path)]
        if os.path.basename(argv[0]) == "less" and len(argv) == 2:
            argv.insert(1, "-R")
        ctx.run_external(argv)

    # -------------------------------------------------------------- events

    def handle(self, event: Any, ctx: Any) -> None:
        key = _key(event)
        if key == "ctrl+c":
            ctx.quit()
            return
        if self.modal is not None:
            result = self.modal.handle(event)
            if result in ("submit", "confirm", "cancel", "close"):
                dialog, then = self.modal, self.on_modal
                self.modal, self.on_modal = None, None
                if then is not None:
                    then(result, dialog)
            return
        if isinstance(event, Paste):
            return
        clicked = self.toolbar.handle(event)
        if clicked:
            self.command(clicked, ctx)
            return
        job = self.selected
        if key == "q":
            ctx.quit()
            return
        if key == "?":
            self.command("help", ctx)
            return
        if key in ("n", "a"):
            self.command("new", ctx)
            return
        if key == "s":
            self.apply(lambda: (None, self.scheduler.sync()), "Synced")
            return
        if key in ("tab", "shift+tab"):
            self.focus = "runs" if self.focus == "jobs" else "jobs"
            return
        m = mouse(event)
        ki = deps.find("kitty_input").module
        if m is not None:
            if m.action == ki.MOUSE_MOVE:
                self.list.handle(event)
                self.runs_list.handle(event)
                return
            if m.action == ki.MOUSE_WHEEL and self.output.rect.contains(m.x, m.y):
                self.output.scroll(3 * m.wheel_y)
                return
            if self.runs_list.rect.contains(m.x, m.y):
                self.focus = "runs"
            elif self.list.rect.contains(m.x, m.y):
                self.focus = "jobs"
        if self.focus == "runs":
            self.handle_runs(event, key, ctx)
            return
        if key in ("l", "right"):
            self.focus = "runs"
            return
        before = self.list.selected
        result = self.list.handle(event)
        if self.list.selected != before:
            self.load_runs(force=True)
        if result == "activate" and self.selected is not None:
            self.task_form(self.selected)
            return
        if result is not None or job is None:
            return
        if key == "e":
            self.task_form(job)
        elif key == "space":
            enabled = not job.enabled
            self.apply(lambda: self.scheduler.update(job.id, enabled=enabled),
                       f"{'Resumed' if enabled else 'Paused'} “{job.name}”")
        elif key == "r":
            self.confirm_run(job)
        elif key in ("d", "delete"):
            self.confirm_delete(job)
        elif key == "u":
            self.show_units(job)
        elif key == "y":
            ctx.copy(job.command)
            self.flash("Copied the command")

    def handle_runs(self, event: Any, key: str | None, ctx: Any) -> None:
        if key in ("h", "left", "escape"):
            self.focus = "jobs"
            return
        if key in ("page_up", "page_down") or key in ("K", "J"):
            step = {"page_up": -10, "page_down": 10, "K": -1, "J": 1}[key]
            self.output.scroll(step)
            return
        before = self.runs_list.selected
        result = self.runs_list.handle(event)
        if self.runs_list.selected != before:
            self.output.follow = True
            self.load_output()
        if result == "activate":
            self.open_pager(ctx)

    def command(self, name: str, ctx: Any) -> None:
        if name == "new":
            if self.error:
                self.flash("The task file can't be read; fix it first", "danger")
                return
            self.task_form(None)
        elif name == "run" and self.selected is not None:
            self.confirm_run(self.selected)
        elif name == "help":
            self.open_modal(HelpOverlay("kilix-scheduled-task", HELP), None)

    def tick(self, now: float, ctx: Any) -> float:
        import time
        if self.modal is None and self.scheduler.store.stamp() != self.stamp:
            self.reload()
        else:
            self.refresh_status()
            self.load_runs()
        if self.message and time.monotonic() >= self.message_until:
            self.message = ""
        if ctx is not None:
            ctx.redraw()   # countdowns move every second
        return 1.0

    # -------------------------------------------------------------- render

    def render(self, p: Any) -> None:
        bounds = p.bounds
        if bounds.w < 50 or bounds.h < 12:
            p.text(1, 0, "kilix-scheduled-task needs at least 50×12", "warn")
            return
        status, body = bounds.take_bottom(1)
        header, body = body.take_top(3)
        self.render_header(p, header)
        wide = bounds.w >= 100
        if wide:
            left, right = body.take_left(max(40, bounds.w * 2 // 5))
        else:
            rows = len(self.jobs) * self.list.row_height + 1
            left, right = body.take_top(max(5, min(rows, body.h // 2)))
        self.render_jobs(p, left.inset(1, 0))
        self.render_detail(p, right.inset(1, 0) if wide else right)
        self.render_status(p, status)
        if self.modal is not None:
            self.modal.render(p, body)

    def render_header(self, p: Any, rect: Rect) -> None:
        x = rect.x + 2
        y = rect.y + 1
        used = p.text(x, y, "Scheduled tasks", "ink", bold=True)
        count = len(self.jobs)
        label = self.scheduler.backend.label
        info = f"{count} task{'s' if count != 1 else ''} · {label}"
        role = "ink_muted"
        if not self.backend_ok:
            info = f"not scheduled: {self.backend_why}"
            role = "warn"
        elif self.sync_error:
            info = f"{label}: {self.sync_error}"
            role = "danger"
        self.toolbar.render(p, x + used + 2, y, limit=rect.right - 1, align_right=True)
        p.text(x + used + 2, y, info, role, limit=rect.right - 36)
        if self.notes and self.backend_ok:
            p.text(x, y + 1, self.notes[0], "warn", limit=rect.right - 1)

    def render_jobs(self, p: Any, rect: Rect) -> None:
        if self.error:
            p.panel(Rect(rect.x, rect.y, rect.w, min(rect.h, 6)), "panel", edge="danger")
            p.text(rect.x + 2, rect.y + 1, "The task file can't be read", "danger", bold=True,
                   limit=rect.right - 2)
            for i, line in enumerate(wrap(self.error, rect.w - 4)[:3]):
                p.text(rect.x + 2, rect.y + 2 + i, line, "ink_muted", limit=rect.right - 2)
            return
        if not self.jobs:
            self.list.rect = rect
            lines = ["No scheduled tasks yet.", "", "Press n to schedule a command:",
                     "a backup every night, a reminder on", "weekdays, a script every 15 minutes."]
            for i, line in enumerate(lines):
                p.text(rect.x + 2, rect.y + 1 + i, line, "ink_muted" if i else "ink",
                       limit=rect.right - 1)
            return
        self.list.render(p, rect, self.draw_job, focused=self.focus == "jobs")

    def draw_job(self, p: Any, index: int, row: Rect, selected: bool, hovered: bool) -> None:
        job = self.jobs[index]
        status = self.statuses.get(job.id)
        state = status.state if status else "never"
        now = self.now()
        x = row.x + 1
        p.dot(x + 1, row.y, STATE_ROLE[state])
        right = row.right - 2
        nxt = until(status.next, now) if status and status.next and job.enabled else ""
        if not job.enabled:
            nxt = "paused"
        if nxt:
            p.text(right - text_width(nxt), row.y, nxt,
                   "ink_faint" if not job.enabled else "ink_muted")
            right -= text_width(nxt) + 2
        p.text(x + 3, row.y, job.name, "ink" if job.enabled else "ink_faint",
               bold=selected, limit=right)
        if row.h > 1:
            meta = describe(job.schedule)
            last = status.last if status else None
            if last and last.get("start") and state != "running":
                started = local_time(last.get("start"))
                meta += f" · {last.get('status')} {ago(started, now)}"
            elif state == "running":
                meta += " · running"
            p.text(x + 3, row.y + 1, meta, STATE_ROLE[state] if state == "failed" else "ink_faint",
                   limit=row.right - 2)

    def render_detail(self, p: Any, rect: Rect) -> None:
        job = self.selected
        box = Rect(rect.x, rect.y, rect.w, rect.h)
        p.panel(box, "panel")
        inner = box.inset(2, 1)
        if job is None:
            p.text(inner.x, inner.y, "Select a task", "ink_faint", limit=inner.right)
            self.runs_list.rect = Rect(0, 0, 0, 0)
            self.output.rect = Rect(0, 0, 0, 0)
            return
        now = self.now()
        status = self.statuses.get(job.id)
        state = status.state if status else "never"
        y = inner.y
        used = p.text(inner.x, y, job.name, "ink", bold=True, limit=inner.right - 14)
        p.chip(inner.x + used + 2, y, STATE_WORD[state], role="tag", ink=STATE_ROLE[state],
               limit=inner.right)
        y += 1
        for line in wrap(job.command, inner.w - 2)[:3]:
            p.text(inner.x, y, line, "accent", limit=inner.right)
            y += 1
        y += 1
        upcoming = job.schedule.next(now, 3) if job.enabled else []
        rows = [("When", describe(job.schedule))]
        if upcoming:
            rows.append(("Next", ", ".join(when(t, now) for t in upcoming)
                         + f"  ({until(upcoming[0], now)})"))
        elif job.schedule.kind == "once":
            rows.append(("Next", "— (it has had its time)"))
        options = [f"in {job.cwd}"]
        if job.timeout:
            options.append(f"limit {job.timeout} s")
        options.append("catches up" if job.catch_up and job.schedule.kind != "login" else "")
        if job.notify_failure:
            options.append("notifies on failure")
        rows.append(("Runs", " · ".join(o for o in options if o)))
        for label, value in rows:
            if y >= inner.bottom:
                return
            p.text(inner.x, y, label, "ink_faint", limit=inner.x + 9)
            p.text(inner.x + 10, y, value, "ink", limit=inner.right)
            y += 1
        y += 1
        if y + 3 >= inner.bottom:
            return
        p.text(inner.x, y, "History", "ink_faint", bold=True)
        y += 1
        history_h = max(1, min(len(self.runs) or 1, 6, (inner.bottom - y) // 3))
        history = Rect(inner.x - 1, y, inner.w + 2, history_h)
        if not self.runs:
            p.text(inner.x, y, "No runs yet. r runs it now.", "ink_faint", limit=inner.right)
            self.runs_list.rect = history
        else:
            self.runs_list.render(p, history, self.draw_run, focused=self.focus == "runs")
        y += history_h + 1
        if y + 1 >= inner.bottom:
            return
        record = self.selected_run
        title = "Output"
        if record is not None:
            started = local_time(record.get("start"))
            title += f" · {when(started, now)}"
            if record.get("truncated"):
                title += f" · first 1 MiB of {record.get('bytes', 0) // 1024} KiB"
        p.text(inner.x, y, title, "ink_faint", bold=True, limit=inner.right)
        y += 1
        out_rect = Rect(inner.x, y, inner.w, inner.bottom - y)
        p.fill(Rect(inner.x - 1, y, inner.w + 2, out_rect.h), "field")
        self.output.render(p, out_rect)
        if record is not None and not self.output.lines:
            p.text(inner.x, y, "(no output)", "ink_faint")

    def draw_run(self, p: Any, index: int, row: Rect, selected: bool, hovered: bool) -> None:
        record = self.runs[index]
        now = self.now()
        status = record.get("status", "?")
        mark, role = RUN_MARK.get(status, ("?", "ink_faint"))
        x = row.x + 1
        p.text(x, row.y, mark, role, bold=True)
        started = local_time(record.get("start"))
        p.text(x + 2, row.y, when(started, now), "ink", limit=x + 20)
        p.text(x + 21, row.y, status, role, limit=x + 30)
        p.text(x + 30, row.y, duration(record.get("duration")), "ink_muted", limit=x + 38)
        extra = []
        if record.get("exit") not in (None, 0):
            extra.append(f"exit {record['exit']}")
        if record.get("trigger") == "manual":
            extra.append("manual")
        if record.get("note"):
            extra.append(str(record["note"]))
        if extra:
            p.text(x + 39, row.y, " · ".join(extra), "ink_faint", limit=row.right - 1)

    def render_status(self, p: Any, rect: Rect) -> None:
        p.fill(rect, "sidebar")
        if self.message:
            p.text(rect.x + 2, rect.y, self.message, self.message_role, limit=rect.right - 1)
            return
        hints = {
            "jobs": "n new · e edit · space pause · r run now · d delete · tab history · ? help · q quit",
            "runs": "j/k runs · enter full output · J/K scroll · tab tasks · q quit",
        }[self.focus]
        p.text(rect.x + 2, rect.y, hints, "ink_faint", limit=rect.right - 1)
