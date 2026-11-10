import os
import random
import re
import shutil
import subprocess
import unittest
from datetime import datetime

from kilix_scheduled_task.schedule import (Schedule, ScheduleError, describe,
                                           parse, parse_cron, to_crontab,
                                           to_oncalendar)

NOW = datetime(2026, 11, 9, 19, 30)  # a Monday evening


def cron_of(text):
    s = parse(text, NOW)
    assert s.kind == "cron", s
    return s.cron


class Words(unittest.TestCase):
    def test_translations(self):
        cases = {
            "08:30": "30 8 * * *",
            "at 8:30": "30 8 * * *",
            "every day at 9am": "0 9 * * *",
            "daily at 9am and 5pm": "0 9,17 * * *",
            "weekdays 8:30": "30 8 * * 1-5",
            "every weekday at 8:30am": "30 8 * * 1-5",
            "weekends at 10": None,  # a bare number is not a time
            "weekends at 10am": "0 10 * * 0,6",
            "mon,wed,fri at 18:00": "0 18 * * 1,3,5",
            "monday and thursday 7pm": "0 19 * * 1,4",
            "mon-fri 9am": "0 9 * * 1-5",
            "sat..sun 10am": "0 10 * * 0,6",
            "fri..mon 6am": "0 6 * * 0,1,5,6",
            "sun 03:00": "0 3 * * 0",
            "sundays at noon": "0 12 * * 0",
            "every 15 minutes": "*/15 * * * *",
            "every minute": "* * * * *",
            "every 2 hours": "0 */2 * * *",
            "every hour": "0 * * * *",
            "hourly": "0 * * * *",
            "daily": "0 0 * * *",
            "weekly": "0 0 * * 0",
            "monthly": "0 0 1 * *",
            "monthly on the 1st at 9am": "0 9 1 * *",
            "on the 15th at 10:00": "0 10 15 * *",
            "1st of every month 9am": "0 9 1 * *",
            "*/5 9-17 * * 1-5": "*/5 9-17 * * 1-5",
            "0 9 1 * MON": "0 9 1 * MON",
            "@daily": "0 0 * * *",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                if expected is None:
                    with self.assertRaises(ScheduleError):
                        parse(text, NOW)
                else:
                    self.assertEqual(cron_of(text), expected)

    def test_once_and_login(self):
        self.assertEqual(parse("once tomorrow 9am", NOW), Schedule("once", at="2026-11-10T09:00"))
        self.assertEqual(parse("once 2026-12-24 18:00", NOW).at, "2026-12-24T18:00")
        self.assertEqual(parse("once at 2026-12-24 at 6pm", NOW).at, "2026-12-24T18:00")
        self.assertEqual(parse("in 10 minutes", NOW).at, "2026-11-09T19:40")
        self.assertEqual(parse("once fri 7am", NOW).at, "2026-11-13T07:00")
        self.assertEqual(parse("once mon 7am", NOW).at, "2026-11-16T07:00")
        self.assertEqual(parse("at login", NOW), Schedule("login"))
        self.assertEqual(parse("@reboot", NOW).kind, "login")
        with self.assertRaises(ScheduleError):
            parse("once 2026-11-01 09:00", NOW)   # already passed
        with self.assertRaises(ScheduleError):
            parse("once 2026-02-30 09:00", NOW)

    def test_errors(self):
        for text in ("", "sometimes", "every 7 minutes", "every 5 hours",
                     "61 * * * *", "* * * * 8", "0 0 30 2 *", "1-0 * * * *",
                     "9:15 and 17:40", "*/0 * * * *"):
            with self.subTest(text=text), self.assertRaises(ScheduleError):
                parse(text, NOW)

    def test_describe(self):
        cases = {
            "30 8 * * 1-5": "weekdays at 08:30",
            "0 9,17 * * *": "every day at 09:00 and 17:00",
            "*/15 * * * *": "every 15 minutes",
            "* * * * *": "every minute",
            "0 * * * *": "every hour",
            "5 * * * *": "every hour at :05",
            "0 */2 * * *": "every 2 hours",
            "0 3 * * 0": "Sundays at 03:00",
            "0 10 * * 0,6": "weekends at 10:00",
            "0 19 * * 1,4": "Mondays and Thursdays at 19:00",
            "0 9 1 * *": "monthly on the 1st at 09:00",
            "0 9 1,15 * *": "monthly on the 1st and 15th at 09:00",
            "0 9 1 * 1": "cron 0 9 1 * 1",
            "0 9 * 6 *": "cron 0 9 * 6 *",
        }
        for cron, expected in cases.items():
            with self.subTest(cron=cron):
                self.assertEqual(describe(Schedule("cron", cron=cron)), expected)
        self.assertEqual(describe(Schedule("once", at="2026-11-10T09:00")),
                         "once, Tue 2026-11-10 09:00")


class NextRuns(unittest.TestCase):
    def runs(self, cron, count=3, after=NOW):
        return [t.strftime("%a %Y-%m-%d %H:%M") for t in parse_cron(cron).next(after, count)]

    def test_examples(self):
        self.assertEqual(self.runs("30 8 * * 1-5"),
                         ["Tue 2026-11-10 08:30", "Wed 2026-11-11 08:30", "Thu 2026-11-12 08:30"])
        self.assertEqual(self.runs("*/20 * * * *"),
                         ["Mon 2026-11-09 19:40", "Mon 2026-11-09 20:00", "Mon 2026-11-09 20:20"])
        # Vixie OR: the 1st of the month or any Monday.
        self.assertEqual(self.runs("0 9 1 * 1", 3),
                         ["Mon 2026-11-16 09:00", "Mon 2026-11-23 09:00", "Mon 2026-11-30 09:00"])
        self.assertEqual(self.runs("0 9 1 * 1", 5)[3:], ["Tue 2026-12-01 09:00", "Mon 2026-12-07 09:00"])
        # A star-prefixed field means AND: odd days that are Mondays.
        self.assertEqual(self.runs("0 9 */2 * 1", 2),
                         ["Mon 2026-11-23 09:00", "Mon 2026-12-07 09:00"])
        self.assertEqual(self.runs("0 0 29 2 *", 2), ["Tue 2028-02-29 00:00", "Sun 2032-02-29 00:00"])
        self.assertEqual(self.runs("59 23 31 12 *", 1), ["Thu 2026-12-31 23:59"])
        self.assertEqual(self.runs("* * * * *", 1, datetime(2026, 12, 31, 23, 59, 30)),
                         ["Fri 2027-01-01 00:00"])

    def test_once(self):
        s = Schedule("once", at="2026-11-10T09:00")
        self.assertEqual(s.next(NOW), [datetime(2026, 11, 10, 9, 0)])
        self.assertEqual(s.next(datetime(2026, 11, 11)), [])
        self.assertEqual(to_crontab(s), "0 9 10 11 *")
        self.assertEqual(to_oncalendar(s), ["2026-11-10 09:00:00"])


class Backends(unittest.TestCase):
    def test_oncalendar_forms(self):
        cases = {
            "30 8 * * 1-5": ["Mon,Tue,Wed,Thu,Fri *-*-* 08:30:00"],
            "*/15 * * * *": ["*-*-* *:00,15,30,45:00"],
            "0 9 1 * 1": ["*-*-01 09:00:00", "Mon *-*-* 09:00:00"],
            "0 3 * * 0": ["Sun *-*-* 03:00:00"],
            "0 10 * * 6,0": ["Sat,Sun *-*-* 10:00:00"],
            "0 9-17 * 1-3 *": ["*-01..03-* 09..17:00:00"],
        }
        for cron, expected in cases.items():
            with self.subTest(cron=cron):
                self.assertEqual(to_oncalendar(Schedule("cron", cron=cron)), expected)
        self.assertEqual(to_oncalendar(Schedule("login")), [])
        self.assertEqual(to_crontab(Schedule("login")), "@reboot")


def _systemd_next(expressions, base, count):
    """systemd's own next elapses for OnCalendar lines (UTC, so no DST)."""
    out = subprocess.run(
        ["systemd-analyze", "calendar", f"--iterations={count}",
         f"--base-time={base:%Y-%m-%d %H:%M:%S} UTC", *expressions],
        capture_output=True, text=True, env=dict(os.environ, TZ="UTC"), timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    times = re.findall(r"(?:Next elapse|Iteration #\d+): \w+ (\d{4}-\d{2}-\d{2} \d{2}:\d{2}):00", out.stdout)
    return sorted(set(datetime.strptime(t, "%Y-%m-%d %H:%M") for t in times))


@unittest.skipUnless(shutil.which("systemd-analyze"), "needs systemd-analyze")
class AgainstSystemd(unittest.TestCase):
    """Our next-run calculator and our OnCalendar translation, checked against
    systemd itself on random cron expressions."""

    def random_field(self, rng, low, high, names=None):
        kind = rng.random()
        if kind < 0.35:
            return "*"
        if kind < 0.5:
            return f"*/{rng.choice([2, 3, 5, 7, 10, 15])}"
        if kind < 0.7:
            a = rng.randint(low, high)
            b = rng.randint(a, high)
            return f"{a}-{b}" + (f"/{rng.randint(1, 3)}" if rng.random() < 0.3 else "")
        values = sorted(rng.sample(range(low, high + 1), rng.randint(1, 4)))
        return ",".join(str(v) for v in values)

    def test_random_expressions(self):
        rng = random.Random(int(os.environ.get("KST_SEED", "2411")))
        base = datetime(2026, 11, 9, 19, 30)
        checked = 0
        while checked < 60:
            fields = [self.random_field(rng, 0, 59), self.random_field(rng, 0, 23),
                      self.random_field(rng, 1, 28), self.random_field(rng, 1, 12),
                      self.random_field(rng, 0, 6)]
            text = " ".join(fields)
            try:
                cron = parse_cron(text)
            except ScheduleError:
                continue
            ours = cron.next(base, 12)
            lines = to_oncalendar(Schedule("cron", cron=text))
            theirs = _systemd_next(lines, base, 12)[:12]
            # With two OR'd lines systemd lists 12 from each; compare the
            # first twelve of the union.
            self.assertEqual(ours, theirs[:len(ours)], f"{text} -> {lines}")
            self.assertEqual(len(ours), 12, text)
            checked += 1


if __name__ == "__main__":
    unittest.main()
