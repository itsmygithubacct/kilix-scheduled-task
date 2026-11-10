"""The window, driven headlessly against a fake systemd."""

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from kilix_pane import render_text
from kilix_pane.runtime import Context
from kitty_input import decode

from kilix_scheduled_task.app import SchedulerApp
from kilix_scheduled_task.backends import SystemdBackend
from kilix_scheduled_task.schedule import parse
from kilix_scheduled_task.service import Scheduler

from test_backends import FakeSystem, temp_paths

NOW = datetime(2026, 11, 9, 19, 30)


class Harness:
    def __init__(self, root, columns=130, rows=36):
        self.paths = temp_paths(root)
        self.fake = FakeSystem()
        self.scheduler = Scheduler(self.paths, backend=SystemdBackend(self.paths, self.fake))
        self.app = SchedulerApp(self.scheduler, now=lambda: NOW)
        self.ctx = Context()
        self.columns, self.rows = columns, rows

    def keys(self, data):
        for event in decode(data.encode() if isinstance(data, str) else data):
            self.app.handle(event, self.ctx)
        return self

    def text(self):
        return "\n".join(render_text(self.app, self.columns, self.rows))


class Window(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.h = Harness(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_empty(self):
        text = self.h.text()
        self.assertIn("No scheduled tasks yet.", text)
        self.assertIn("systemd user timers", text)

    def test_new_task_previews_confirms_and_syncs(self):
        h = self.h.keys("n")
        self.assertIn("New task", h.text())
        h.keys("Backup photos\t").keys("weekdays 2am")
        self.assertIn("↳ weekdays at 02:00 — next tomorrow 02:00, Wed 02:00, Thu 02:00", h.text())
        h.keys("\x15every 7 minutes")
        self.assertIn("✗ every N minutes needs N to divide 60", h.text())
        h.keys("\x15weekdays 2am\t").keys("rsync -a ~/Pictures /mnt/backup")
        h.keys("\x13")   # ctrl+s
        self.assertIn("Schedule “Backup photos”?", h.text())
        self.assertIn("Runs: rsync -a ~/Pictures /mnt/backup", h.text())
        self.assertEqual(h.scheduler.load().jobs, [])     # nothing until confirmed
        h.keys("\r")
        jobs = h.scheduler.load().jobs
        self.assertEqual([(j.id, j.schedule.cron) for j in jobs], [("backup-photos", "0 2 * * 1-5")])
        self.assertTrue((self.h.paths.units / "kilix-task-backup-photos.timer").exists())
        self.assertIn(["enable", "--now", "kilix-task-backup-photos.timer"], self.h.fake.systemctl())
        text = h.text()
        self.assertIn("Scheduled “Backup photos”", text)
        self.assertIn("in 6h 30m", text)

    def test_form_refuses_bad_values(self):
        h = self.h.keys("n").keys("\x13")
        self.assertIn("a task needs a name", h.text())
        h.keys("X\t").keys("soonish\t").keys("true").keys("\x13")
        self.assertIn("can't read 'soonish'", h.text())
        self.assertEqual(h.scheduler.load().jobs, [])

    def test_pause_resume_run_delete(self):
        self.h.scheduler.add("Ping", "echo ping", parse("every 15 minutes"))
        h = Harness(self.dir.name)
        h.fake = self.h.fake
        h.keys(" ")
        self.assertFalse(h.scheduler.load().jobs[0].enabled)
        self.assertIn("paused", h.text())
        h.keys(" ")
        self.assertTrue(h.scheduler.load().jobs[0].enabled)
        h.keys("r")
        self.assertIn("Run “Ping” now?", h.text())
        h.keys("\r")
        self.assertIn(["start", "--no-block", "kilix-task-ping.service"],
                      [c[2:] for c in h.scheduler.backend.run.calls if c[:2] == ["systemctl", "--user"]])
        self.assertEqual(h.app.focus, "runs")   # run now shows its history
        h.keys("\x1b")
        self.assertEqual(h.app.focus, "jobs")
        h.keys("d")
        self.assertIn("Delete “Ping”?", h.text())
        h.keys("\r")
        self.assertEqual(h.scheduler.load().jobs, [])

    def test_history_and_output_follow_the_selection(self):
        import subprocess
        job, _ = self.h.scheduler.add("Echo", "echo first-line; echo second-line", parse("daily"))
        argv = self.h.scheduler.backend.runner_argv(job, "manual")
        subprocess.run([sys.executable, *argv[1:]], check=True, timeout=60)
        failing, _ = self.h.scheduler.add("Fails", "echo bad-output; exit 4", parse("daily"))
        argv = self.h.scheduler.backend.runner_argv(failing, "manual")
        subprocess.run([sys.executable, *argv[1:]], timeout=60)
        h = Harness(self.dir.name)
        text = h.text()
        self.assertIn("✓", text)
        self.assertIn("manual", text)
        self.assertIn("second-line", text)
        self.assertNotIn("bad-output", text)
        h.keys("j")                      # select the failing task
        text = h.text()
        self.assertIn("✗", text)
        self.assertIn("exit 4", text)
        self.assertIn("bad-output", text)
        self.assertNotIn("second-line", text)

    def test_tick_reloads_other_writers(self):
        h = self.h
        Scheduler(h.paths, backend=SystemdBackend(h.paths, FakeSystem())).add(
            "From CLI", "true", parse("hourly"))
        h.app.tick(0, h.ctx)
        self.assertIn("From CLI", h.text())

    def test_sync_errors_are_shown(self):
        self.h.fake.fail.add("enable --now")
        h = self.h.keys("n").keys("Fails\t").keys("hourly\t").keys("true").keys("\x13").keys("\r")
        self.assertIn("the backend said", h.text())
        self.assertEqual(len(h.scheduler.load().jobs), 1)   # the definition is kept

    def test_small_and_narrow(self):
        self.h.scheduler.add("Ping", "echo ping", parse("every 15 minutes"))
        for columns, rows in ((80, 24), (60, 16), (50, 12)):
            h = Harness(self.dir.name, columns, rows)
            self.assertIn("Ping", h.text(), (columns, rows))
        self.assertIn("needs at least", Harness(self.dir.name, 40, 10).text())

    def test_quit(self):
        self.h.keys("q")
        self.assertTrue(self.h.ctx.quitting)


if __name__ == "__main__":
    unittest.main()
