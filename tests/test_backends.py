"""The systemd and cron backends against a fake system, and the runner for real."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from kilix_scheduled_task import runner
from kilix_scheduled_task.backends import (CRON_BEGIN, CRON_END, CronBackend,
                                           SystemdBackend, cron_quote,
                                           systemd_quote)
from kilix_scheduled_task.jobs import Jobs, Paths, Store
from kilix_scheduled_task.schedule import Schedule, parse
from kilix_scheduled_task.service import Scheduler


class FakeSystem:
    """systemctl --user, crontab and loginctl, recorded."""

    def __init__(self):
        self.calls = []
        self.crontab = None
        self.fail = set()

    def __call__(self, argv, stdin=None):
        argv = list(argv)
        self.calls.append(argv)
        out, err, code = "", "", 0
        if argv[0] == "crontab":
            if argv[1] == "-l":
                if self.crontab is None:
                    err, code = "no crontab for tester\n", 1
                else:
                    out = self.crontab
            elif argv[1] == "-":
                self.crontab = stdin
        elif argv[0] == "loginctl":
            out = "no\n"
        elif argv[0] == "pgrep":
            code = 1
        if " ".join(argv[2:4]) in self.fail or (len(argv) > 2 and argv[2] in self.fail):
            code, err = 1, "simulated failure"
        return subprocess.CompletedProcess(argv, code, out, err)

    def systemctl(self):
        return [c[2:] for c in self.calls if c[:2] == ["systemctl", "--user"]]


def temp_paths(root):
    root = Path(root)
    return Paths(jobs=root / "config" / "tasks.json", state=root / "state",
                 data=root / "data", units=root / "units")


class Systemd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.paths = temp_paths(self.dir.name)
        self.fake = FakeSystem()
        self.backend = SystemdBackend(self.paths, self.fake, prefix="kilix-task-")
        self.jobs = Jobs()
        self.a = self.jobs.add("Backup photos", "rsync -a ~/Pictures /mnt/backup",
                               parse("weekdays 02:00"))
        self.b = self.jobs.add("Monthly report", "make report", parse("0 9 1 * 1"),
                               catch_up=False)

    def tearDown(self):
        self.dir.cleanup()

    def test_first_sync_writes_units_and_enables(self):
        report = self.backend.sync(self.jobs.jobs)
        self.assertEqual(report.errors, [])
        timer = (self.paths.units / "kilix-task-backup-photos.timer").read_text()
        self.assertIn("OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 02:00:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("X-KilixScheduledTask=backup-photos", timer)
        report_timer = (self.paths.units / "kilix-task-monthly-report.timer").read_text()
        self.assertIn("OnCalendar=*-*-01 09:00:00\nOnCalendar=Mon *-*-* 09:00:00", report_timer)
        self.assertNotIn("Persistent", report_timer)
        service = (self.paths.units / "kilix-task-backup-photos.service").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn(f"ExecStart=", service)
        self.assertIn(f"run --jobs {self.paths.jobs} --state {self.paths.state} backup-photos", service)
        self.assertIn("# Runs: rsync -a ~/Pictures /mnt/backup", service)
        self.assertTrue(self.paths.runner.exists())
        calls = self.fake.systemctl()
        self.assertIn(["daemon-reload"], calls)
        self.assertIn(["enable", "--now", "kilix-task-backup-photos.timer"], calls)
        self.assertIn(["restart", "kilix-task-backup-photos.timer"], calls)

    def test_second_sync_changes_nothing(self):
        self.backend.sync(self.jobs.jobs)
        self.fake.calls.clear()
        report = self.backend.sync(self.jobs.jobs)
        self.assertEqual((report.changed, report.removed), ([], []))
        calls = self.fake.systemctl()
        self.assertNotIn(["daemon-reload"], calls)
        self.assertNotIn(["restart", "kilix-task-backup-photos.timer"], calls)
        self.assertIn(["enable", "--now", "kilix-task-backup-photos.timer"], calls)

    def test_schedule_change_restarts_disable_and_remove(self):
        self.backend.sync(self.jobs.jobs)
        self.jobs.update("backup-photos", schedule=parse("sun 03:00"))
        self.jobs.update("monthly-report", enabled=False)
        self.fake.calls.clear()
        self.backend.sync(self.jobs.jobs)
        calls = self.fake.systemctl()
        self.assertIn(["restart", "kilix-task-backup-photos.timer"], calls)
        self.assertIn(["disable", "--now", "kilix-task-monthly-report.timer"], calls)
        self.jobs.remove("monthly-report")
        self.fake.calls.clear()
        report = self.backend.sync(self.jobs.jobs)
        self.assertEqual(sorted(report.removed), ["kilix-task-monthly-report.service",
                                                  "kilix-task-monthly-report.timer"])
        self.assertFalse((self.paths.units / "kilix-task-monthly-report.timer").exists())
        self.assertIn(["disable", "--now", "kilix-task-monthly-report.timer"], self.fake.systemctl())
        self.assertIn(["daemon-reload"], self.fake.systemctl())

    def test_foreign_units_are_never_touched(self):
        self.paths.units.mkdir(parents=True)
        foreign = self.paths.units / "kilix-task-backup-photos.timer"
        foreign.write_text("[Timer]\nOnCalendar=daily\n")
        other = self.paths.units / "kilix-task-something-else.service"
        other.write_text("[Service]\nExecStart=/bin/true\n")
        unrelated = self.paths.units / "dogbook.service"
        unrelated.write_text("[Service]\nExecStart=/bin/true\n")
        report = self.backend.sync(self.jobs.jobs)
        self.assertTrue(any("not made by kilix-scheduled-task" in e for e in report.errors))
        self.assertEqual(foreign.read_text(), "[Timer]\nOnCalendar=daily\n")
        self.assertTrue(other.exists())
        self.assertTrue(unrelated.exists())
        self.assertNotIn(["enable", "--now", "kilix-task-backup-photos.timer"], self.fake.systemctl())

    def test_login_and_once(self):
        jobs = Jobs()
        jobs.add("At login", "true", Schedule("login"))
        jobs.add("Once", "true", Schedule("once", at="2030-01-02T03:04"))
        self.backend.sync(jobs.jobs)
        login = (self.paths.units / "kilix-task-at-login.timer").read_text()
        self.assertIn("OnStartupSec=30s", login)
        self.assertNotIn("OnCalendar", login)
        once = (self.paths.units / "kilix-task-once.timer").read_text()
        self.assertIn("OnCalendar=2030-01-02 03:04:00", once)

    def test_errors_are_reported(self):
        self.fake.fail.add("enable --now")
        report = self.backend.sync(self.jobs.jobs)
        self.assertEqual(len(report.errors), 2)

    def test_run_now_marks_manual(self):
        self.backend.run_now(self.a)
        self.assertTrue((self.paths.task_dir("backup-photos") / "manual-request").exists())
        self.assertIn(["start", "--no-block", "kilix-task-backup-photos.service"],
                      self.fake.systemctl())

    def test_linger_note(self):
        os.environ.setdefault("USER", "tester")
        self.assertIn("enable-linger", self.backend.notes()[0])

    def test_quoting(self):
        self.assertEqual(systemd_quote("/usr/bin/python3"), "/usr/bin/python3")
        self.assertEqual(systemd_quote("/srv/a b/x"), '"/srv/a b/x"')
        self.assertEqual(systemd_quote("100%$HOME"), '"100%%$$HOME"')
        self.assertEqual(systemd_quote('say "hi"'), '"say \\"hi\\""')
        self.assertEqual(cron_quote("/a b/50%"), "'/a b/50\\%'")


class Cron(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.paths = temp_paths(self.dir.name)
        self.fake = FakeSystem()
        self.backend = CronBackend(self.paths, self.fake, prefix="kilix-task-")
        self.jobs = Jobs()
        self.jobs.add("Backup", "rsync -a src dst", parse("weekdays 02:00"))

    def tearDown(self):
        self.dir.cleanup()

    def test_block_is_added_kept_apart_and_replaced_in_place(self):
        self.fake.crontab = "MAILTO=me\n# mine\n0 5 * * * /usr/bin/true\n"
        self.backend.sync(self.jobs.jobs)
        text = self.fake.crontab
        self.assertTrue(text.startswith("MAILTO=me\n# mine\n0 5 * * * /usr/bin/true\n\n" + CRON_BEGIN))
        self.assertIn(f"0 2 * * 1-5 ", text)
        self.assertIn(f" run --jobs {self.paths.jobs} --state {self.paths.state} backup # kilix-task-backup", text)
        self.assertTrue(text.endswith(CRON_END + "\n"))
        # A line the user adds after the block survives an update in place.
        self.fake.crontab = text + "@hourly /usr/bin/date\n"
        self.jobs.update("backup", enabled=False)
        self.backend.sync(self.jobs.jobs)
        text = self.fake.crontab
        self.assertIn("# (disabled) 0 2 * * 1-5", text)
        self.assertTrue(text.endswith(CRON_END + "\n@hourly /usr/bin/date\n"))
        self.assertEqual(text.count(CRON_BEGIN), 1)
        # No tasks: the block goes, the user's lines stay.
        self.jobs.remove("backup")
        self.backend.sync(self.jobs.jobs)
        self.assertEqual(self.fake.crontab,
                         "MAILTO=me\n# mine\n0 5 * * * /usr/bin/true\n\n@hourly /usr/bin/date\n")

    def test_no_crontab_yet_and_unchanged_sync_does_not_write(self):
        self.backend.sync(self.jobs.jobs)
        self.assertTrue(self.fake.crontab.startswith(CRON_BEGIN))
        writes = sum(1 for c in self.fake.calls if c[:2] == ["crontab", "-"])
        self.backend.sync(self.jobs.jobs)
        self.assertEqual(writes, sum(1 for c in self.fake.calls if c[:2] == ["crontab", "-"]))

    def test_broken_block_is_left_alone(self):
        self.fake.crontab = f"{CRON_BEGIN}\n* * * * * x\n"
        report = self.backend.sync(self.jobs.jobs)
        self.assertTrue(report.errors)
        self.assertEqual(self.fake.crontab, f"{CRON_BEGIN}\n* * * * * x\n")

    def test_login_and_once_lines(self):
        jobs = Jobs()
        jobs.add("Boot", "true", Schedule("login"))
        jobs.add("Once", "true", Schedule("once", at="2030-01-02T03:04"))
        self.assertTrue(self.backend.line(jobs.jobs[0]).startswith("@reboot "))
        self.assertTrue(self.backend.line(jobs.jobs[1]).startswith("4 3 2 1 * "))


class Runner(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.paths = temp_paths(self.dir.name)
        self.store = Store(self.paths.jobs)

    def tearDown(self):
        self.dir.cleanup()

    def add(self, command, **options):
        schedule = options.pop("schedule", parse("daily"))
        with self.store.transaction() as jobs:
            return jobs.add("Job", command, schedule, **options)

    def run_job(self, job_id, *extra):
        argv = [sys.executable, str(Path(runner.__file__)), "run", "--jobs", str(self.paths.jobs),
                "--state", str(self.paths.state), *extra, job_id]
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    def runs(self, job_id):
        path = self.paths.task_dir(job_id) / "runs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def log(self, job_id, record):
        return (self.paths.task_dir(job_id) / "logs" / record["log"]).read_text()

    def test_ok_and_failed(self):
        job = self.add("echo hello $KST_TASK_ID; echo oops >&2; pwd")
        self.assertEqual(self.run_job(job.id).returncode, 0)
        record = self.runs(job.id)[0]
        self.assertEqual((record["status"], record["exit"], record["trigger"]), ("ok", 0, "schedule"))
        text = self.log(job.id, record)
        self.assertIn("hello job", text)
        self.assertIn("oops", text)
        self.assertIn(str(Path.home()), text)
        failing = self.add("exit 3")
        self.assertEqual(self.run_job(failing.id).returncode, 3)
        self.assertEqual(self.runs(failing.id)[0]["status"], "failed")
        self.assertEqual(self.runs(failing.id)[0]["exit"], 3)

    def test_output_is_capped_but_counted(self):
        job = self.add(f"{sys.executable} -c \"import sys; sys.stdout.write('x' * 3000000)\"")
        self.run_job(job.id)
        record = self.runs(job.id)[0]
        self.assertTrue(record["truncated"])
        self.assertEqual(record["bytes"], 3000000)
        size = (self.paths.task_dir(job.id) / "logs" / record["log"]).stat().st_size
        self.assertLess(size, runner.MAX_LOG + 4096)

    def test_timeout_stops_the_whole_group(self):
        job = self.add("sleep 30 & sleep 30; echo never", timeout=1)
        started = time.monotonic()
        self.assertNotEqual(self.run_job(job.id).returncode, 0)
        self.assertLess(time.monotonic() - started, 10)
        record = self.runs(job.id)[0]
        self.assertEqual(record["status"], "timeout")
        self.assertNotIn("\nnever\n", self.log(job.id, record))

    def test_background_child_does_not_hold_the_run_open(self):
        job = self.add("sleep 20 >/dev/null 2>&1 & sleep 20 & echo started")
        started = time.monotonic()
        self.run_job(job.id)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(self.runs(job.id)[0]["status"], "ok")

    def test_overlap_is_skipped(self):
        job = self.add("sleep 2")
        first = threading.Thread(target=self.run_job, args=(job.id,))
        first.start()
        deadline = time.monotonic() + 5
        while not (self.paths.task_dir(job.id) / "running.json").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.assertTrue(Scheduler(self.paths, backend=SystemdBackend(self.paths, FakeSystem())).running(job))
        self.run_job(job.id)
        first.join()
        statuses = sorted(r["status"] for r in self.runs(job.id))
        self.assertEqual(statuses, ["ok", "skipped"])
        self.assertFalse((self.paths.task_dir(job.id) / "running.json").exists())

    def test_manual_marker_disabled_once_and_missing(self):
        job = self.add("true")
        self.paths.task_dir(job.id).mkdir(parents=True)
        (self.paths.task_dir(job.id) / "manual-request").touch()
        self.run_job(job.id)
        self.assertEqual(self.runs(job.id)[-1]["trigger"], "manual")
        self.assertFalse((self.paths.task_dir(job.id) / "manual-request").exists())
        off = self.add("true", enabled=False)
        self.run_job(off.id)
        self.assertEqual(self.runs(off.id), [])
        self.run_job(off.id, "--trigger", "manual")
        self.assertEqual(len(self.runs(off.id)), 1)
        other_year = (datetime.now() + timedelta(days=400)).strftime("%Y-%m-%dT%H:%M")
        once = self.add("true", schedule=Schedule("once", at=other_year))
        self.run_job(once.id)
        self.assertEqual(self.runs(once.id), [])
        missing = self.run_job("no-such-task")
        self.assertEqual(missing.returncode, 0)
        self.assertIn("no task", missing.stderr)

    def test_logs_are_pruned(self):
        job = self.add("echo run", keep=2)
        for _ in range(4):
            self.run_job(job.id)
            time.sleep(1.05)   # log names have one-second resolution
        logs = sorted((self.paths.task_dir(job.id) / "logs").iterdir())
        self.assertEqual(len(logs), 2)
        self.assertEqual(len(self.runs(job.id)), 4)


if __name__ == "__main__":
    unittest.main()
