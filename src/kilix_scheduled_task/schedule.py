"""When a task runs: words in, a cron expression out, and back again.

A schedule is one of

- ``cron``: a five-field cron expression (minute hour day-of-month month
  day-of-week), the one language both backends speak. Words such as
  ``weekdays 8:30`` or ``every 15 minutes`` are translated into it;
- ``once``: a single local date and time;
- ``login``: when the user's session manager starts (systemd), or at boot
  (cron's ``@reboot``).

Day-of-month and day-of-week follow Vixie cron: when both are restricted a
day matches either one, so ``0 9 1 * mon`` runs on the first of the month
*and* on every Monday. `to_oncalendar` reproduces that in systemd with two
``OnCalendar=`` lines, which systemd ORs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterator

__all__ = [
    "Cron",
    "Schedule",
    "ScheduleError",
    "describe",
    "parse",
    "parse_cron",
    "to_crontab",
    "to_oncalendar",
]


class ScheduleError(ValueError):
    pass


MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
               "oct", "nov", "dec"]
DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
DOW_TITLES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday"]
SYSTEMD_DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# (name, low, high) for the five fields.
FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31),
          ("month", 1, 12), ("day of week", 0, 7)]


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]   # 0 = Sunday
    day_star: bool             # the day-of-month field began with "*"
    weekday_star: bool
    text: str

    def day_matches(self, day: date) -> bool:
        dom = day.day in self.days
        dow = (day.isoweekday() % 7) in self.weekdays
        if self.day_star or self.weekday_star:
            return dom and dow
        return dom or dow

    def times(self, after: datetime) -> Iterator[datetime]:
        """Every run strictly after `after`, in order (naive local time)."""
        t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        minutes = sorted(self.minutes)
        hours = sorted(self.hours)
        steps = 0
        while steps < 200_000:
            steps += 1
            if t.month not in self.months:
                year = t.year + (t.month == 12)
                t = datetime(year, t.month % 12 + 1, 1)
                if year > after.year + 30:
                    return
                continue
            if not self.day_matches(t.date()):
                t = datetime(t.year, t.month, t.day) + timedelta(days=1)
                continue
            if t.hour not in self.hours:
                later = [h for h in hours if h > t.hour]
                if not later:
                    t = datetime(t.year, t.month, t.day) + timedelta(days=1)
                else:
                    t = t.replace(hour=later[0], minute=0)
                continue
            if t.minute not in self.minutes:
                later = [m for m in minutes if m > t.minute]
                if not later:
                    t = t.replace(minute=0) + timedelta(hours=1)
                else:
                    t = t.replace(minute=later[0])
                continue
            yield t
            t += timedelta(minutes=1)

    def next(self, after: datetime, count: int = 1) -> list[datetime]:
        out = []
        for moment in self.times(after):
            out.append(moment)
            if len(out) >= count:
                break
        return out


def _value(text: str, low: int, high: int, names: list[str] | None,
           offset: int) -> int:
    lower = text.lower()
    if names and lower[:3] in names and lower.isalpha():
        return names.index(lower[:3]) + offset
    if not text.isdigit():
        raise ScheduleError(f"{text!r} is not a number")
    number = int(text)
    if not low <= number <= high:
        raise ScheduleError(f"{number} is outside {low}-{high}")
    return number


def _field(text: str, index: int) -> frozenset[int]:
    name, low, high = FIELDS[index]
    names = MONTH_NAMES if index == 3 else DOW_NAMES if index == 4 else None
    offset = 1 if index == 3 else 0
    values: set[int] = set()
    for item in text.split(","):
        if not item:
            raise ScheduleError(f"empty item in the {name} field")
        base, _, step_text = item.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"bad step {step_text!r} in the {name} field")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, b = base.split("-", 1)
            start = _value(a, low, high, names, offset)
            end = _value(b, low, high, names, offset)
            if end < start:
                raise ScheduleError(f"range {base} runs backwards in the {name} field")
        else:
            start = _value(base, low, high, names, offset)
            end = high if step_text else start
        values.update(range(start, end + 1, step))
    if index == 4 and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
          "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
          "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
          "@hourly": "0 * * * *"}


def parse_cron(text: str) -> Cron:
    text = " ".join(text.split())
    text = MACROS.get(text.lower(), text)
    parts = text.split(" ")
    if len(parts) != 5:
        raise ScheduleError("a cron expression has five fields: minute hour "
                            "day-of-month month day-of-week")
    fields = [_field(part, i) for i, part in enumerate(parts)]
    cron = Cron(*fields, day_star=parts[2].startswith("*"),
                weekday_star=parts[4].startswith("*"), text=text)
    if not cron.next(datetime(2000, 1, 1), 1):
        raise ScheduleError(f"{text} never runs")
    return cron


@dataclass(frozen=True)
class Schedule:
    kind: str                  # "cron", "once" or "login"
    cron: str = ""             # for "cron"
    at: str = ""               # "YYYY-MM-DDTHH:MM" for "once"

    def to_dict(self) -> dict[str, str]:
        out = {"kind": self.kind}
        if self.kind == "cron":
            out["cron"] = self.cron
        elif self.kind == "once":
            out["at"] = self.at
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Schedule":
        kind = data.get("kind")
        if kind == "cron":
            parse_cron(str(data.get("cron", "")))
            return cls("cron", cron=" ".join(str(data["cron"]).split()))
        if kind == "once":
            at = str(data.get("at", ""))
            try:
                datetime.strptime(at, "%Y-%m-%dT%H:%M")
            except ValueError:
                raise ScheduleError(f"bad time {at!r}") from None
            return cls("once", at=at)
        if kind == "login":
            return cls("login")
        raise ScheduleError(f"unknown schedule kind {kind!r}")

    def next(self, after: datetime, count: int = 1) -> list[datetime]:
        if self.kind == "cron":
            return parse_cron(self.cron).next(after, count)
        if self.kind == "once":
            moment = datetime.strptime(self.at, "%Y-%m-%dT%H:%M")
            return [moment] if moment > after else []
        return []

    def describe(self) -> str:
        return describe(self)


# ------------------------------------------------------------------- words

_TIME = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?$|noon$|midnight$")


def _time(text: str) -> time | None:
    text = text.strip().lower().replace(".", "")
    match = _TIME.match(text)
    if not match:
        return None
    if text == "noon":
        return time(12, 0)
    if text == "midnight":
        return time(0, 0)
    hour, minute, meridiem = int(match[1]), int(match[2] or 0), match[3]
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem.startswith("p") else 0)
    elif match[2] is None:
        return None
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _times(text: str) -> list[time] | None:
    """``9am``, ``08:30``, ``9am and 5pm``, ``9:00, 13:00``."""
    parts = [p for p in re.split(r"\s*(?:,|\band\b|&)\s*", text.strip()) if p]
    out = []
    for part in parts:
        clock = _time(part)
        if clock is None:
            return None
        out.append(clock)
    return out or None


_DAY_WORDS = {
    "day": set(range(7)), "days": set(range(7)), "daily": set(range(7)),
    "everyday": set(range(7)),
    "weekday": {1, 2, 3, 4, 5}, "weekdays": {1, 2, 3, 4, 5},
    "weekend": {0, 6}, "weekends": {0, 6},
}
_DOW_ALIASES = {"sun": 0, "mon": 1, "tue": 2, "tues": 2, "wed": 3, "weds": 3,
                "thu": 4, "thur": 4, "thurs": 4, "fri": 5, "sat": 6}


def _weekday(word: str) -> int | None:
    word = word.lower().rstrip("s") if word.lower() not in ("tues", "thurs", "weds") else word.lower()
    for full_index, full in enumerate(DOW_TITLES):
        if word == full.lower():
            return full_index
    return _DOW_ALIASES.get(word)


def _days(text: str) -> set[int] | None:
    """``weekdays``, ``mon``, ``mon,wed,fri``, ``mon-fri``, ``mon..fri``,
    ``monday and thursday``, ``sat & sun``."""
    text = text.strip().lower()
    if text in _DAY_WORDS:
        return set(_DAY_WORDS[text])
    out: set[int] = set()
    for part in re.split(r"\s*(?:,|\band\b|&|\s)\s*", text):
        if not part:
            continue
        span = re.fullmatch(r"([a-z]+)\s*(?:-|\.\.|to|through|thru)\s*([a-z]+)", part)
        if span:
            a, b = _weekday(span[1]), _weekday(span[2])
            if a is None or b is None:
                return None
            day = a
            while True:
                out.add(day)
                if day == b:
                    break
                day = (day + 1) % 7
            continue
        day = _weekday(part)
        if day is None:
            return None
        out.add(day)
    return out or None


def _cron_list(values: set[int]) -> str:
    ordered = sorted(values)
    runs: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        if j - i >= 2:
            runs.append(f"{ordered[i]}-{ordered[j]}")
        else:
            runs.extend(str(v) for v in ordered[i:j + 1])
        i = j + 1
    return ",".join(runs)


def _at(clocks: list[time], days: str = "*", dom: str = "*") -> str:
    minutes = {c.minute for c in clocks}
    if len(minutes) == 1:
        return f"{minutes.pop()} {_cron_list({c.hour for c in clocks})} {dom} * {days}"
    if len(clocks) == 1:
        c = clocks[0]
        return f"{c.minute} {c.hour} {dom} * {days}"
    raise ScheduleError("several times in one schedule need the same minutes "
                        "(9:00 and 17:00 work; 9:15 and 17:40 need two tasks)")


def _ordinal(text: str) -> int | None:
    match = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", text.strip().lower())
    if match and 1 <= int(match[1]) <= 31:
        return int(match[1])
    return None


def _once(text: str, now: datetime) -> datetime | None:
    text = text.strip().lower()
    match = re.fullmatch(r"in\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)", text)
    if match:
        n, unit = int(match[1]), match[2][0]
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return (now + delta).replace(second=0, microsecond=0)
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:\s+at\s+|[t ]+)(.+)", text)
    day: date | None = None
    clock_text = ""
    if match:
        try:
            day = date.fromisoformat(match[1])
        except ValueError:
            raise ScheduleError(f"no such date {match[1]}") from None
        clock_text = match[2]
    else:
        match = re.fullmatch(r"(today|tomorrow|tonight)(?:\s+at)?\s+(.+)", text)
        if match:
            day = now.date() + timedelta(days=1 if match[1] == "tomorrow" else 0)
            clock_text = match[2]
        else:
            match = re.fullmatch(r"(?:next\s+)?([a-z]+)(?:\s+at)?\s+(.+)", text)
            if match and _weekday(match[1]) is not None and match[1] not in _DAY_WORDS:
                target = _weekday(match[1])
                ahead = (target - (now.isoweekday() % 7)) % 7 or 7
                day = now.date() + timedelta(days=ahead)
                clock_text = match[2]
    if day is None:
        return None
    clock = _time(clock_text)
    if clock is None:
        raise ScheduleError(f"not a time: {clock_text!r}")
    return datetime.combine(day, clock)


def parse(text: str, now: datetime | None = None) -> Schedule:
    """Words or a cron expression to a `Schedule`.

    ``08:30`` · ``every day at 9am`` · ``weekdays 8:30`` · ``mon,wed,fri at
    18:00`` · ``sat..sun 10am`` · ``every 15 minutes`` · ``every 2 hours`` ·
    ``hourly`` · ``daily`` · ``weekly`` · ``monthly`` · ``monthly on the 1st
    at 9am`` · ``on the 15th at 10:00`` · ``once tomorrow 9am`` · ``once
    2026-12-24 18:00`` · ``once in 10 minutes`` · ``at login`` · or cron:
    ``*/5 9-17 * * 1-5``, ``@daily``.
    """
    now = now or datetime.now()
    raw = " ".join(text.strip().split())
    lower = raw.lower()
    if not lower:
        raise ScheduleError("say when: e.g. weekdays 8:30, every 15 minutes, "
                            "once tomorrow 9am")
    if lower in ("at login", "on login", "login", "at startup", "on startup",
                 "@reboot", "at boot", "on boot"):
        return Schedule("login")
    if lower.startswith("once") or lower.startswith("in "):
        rest = lower.removeprefix("once").strip().removeprefix("at ").strip() \
            if lower.startswith("once") else lower
        moment = _once(rest, now)
        if moment is None:
            raise ScheduleError(f"not a time: {rest!r} (try tomorrow 9am, "
                                "2026-12-24 18:00, in 10 minutes)")
        if moment <= now:
            raise ScheduleError(f"{moment:%Y-%m-%d %H:%M} has already passed")
        return Schedule("once", at=moment.strftime("%Y-%m-%dT%H:%M"))
    cron_error: ScheduleError | None = None
    if lower in MACROS or re.fullmatch(r"[\d*/,\-a-z]+( [\d*/,\-a-z]+){4}", lower):
        try:
            return Schedule("cron", cron=parse_cron(raw).text)
        except ScheduleError as error:
            cron_error = error   # perhaps words after all ("sat sun at 10 am")
    try:
        return _parse_words(raw, lower)
    except ScheduleError:
        if cron_error is not None:
            raise cron_error from None
        raise


def _parse_words(raw: str, lower: str) -> Schedule:
    words = {"hourly": "0 * * * *", "daily": "0 0 * * *", "weekly": "0 0 * * 0",
             "monthly": "0 0 1 * *", "yearly": "0 0 1 1 *", "annually": "0 0 1 1 *",
             "every minute": "* * * * *", "every hour": "0 * * * *"}
    if lower in words:
        return Schedule("cron", cron=words[lower])
    match = re.fullmatch(r"every\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)", lower)
    if match:
        n, unit = int(match[1]), match[2][0]
        if unit == "m":
            if n < 1 or 60 % n:
                raise ScheduleError("every N minutes needs N to divide 60 (cron "
                                    "starts counting again each hour); write a "
                                    "cron expression for anything else")
            return Schedule("cron", cron="* * * * *" if n == 1 else f"*/{n} * * * *")
        if n < 1 or 24 % n:
            raise ScheduleError("every N hours needs N to divide 24")
        return Schedule("cron", cron="0 * * * *" if n == 1 else f"0 */{n} * * *")
    # monthly on the 1st at 9am / on the 15th at 10:00 / 1st of every month 9am
    match = re.fullmatch(
        r"(?:every month|monthly)?\s*(?:on\s+)?(?:the\s+)?(\d{1,2}(?:st|nd|rd|th)?)"
        r"(?:\s+of\s+(?:every|each|the)\s+month)?(?:\s+at)?(?:\s+(.+))?", lower)
    if match and (lower.startswith(("monthly", "every month", "on ")) or "month" in lower):
        dom = _ordinal(match[1])
        if dom is None:
            raise ScheduleError(f"not a day of the month: {match[1]}")
        clocks = _times(match[2]) if match[2] else [time(0, 0)]
        if clocks is None:
            raise ScheduleError(f"not a time: {match[2]!r}")
        return Schedule("cron", cron=_at(clocks, dom=str(dom)))
    # [every] DAYS [at] TIMES, or TIMES alone, or "daily at TIMES"
    body = lower.removeprefix("every ").strip()
    if body.startswith(("day at ", "daily at ", "day ", "daily ")):
        body = body.split(" ", 1)[1].removeprefix("at ").strip()
        clocks = _times(body)
        if clocks:
            return Schedule("cron", cron=_at(clocks))
    clocks = _times(body.removeprefix("at ").strip())
    if clocks:
        return Schedule("cron", cron=_at(clocks))
    split = re.fullmatch(r"(.+?)(?:\s+at)?\s+(\d.*|noon|midnight)", body)
    if split:
        days = _days(split[1])
        clocks = _times(split[2])
        if days is not None and clocks:
            day_field = "*" if days == set(range(7)) else _cron_list(days)
            return Schedule("cron", cron=_at(clocks, day_field))
    days = _days(body)
    if days is not None:
        day_field = "*" if days == set(range(7)) else _cron_list(days)
        return Schedule("cron", cron=f"0 0 * * {day_field}")
    raise ScheduleError(f"can't read {raw!r} as a schedule (try weekdays 8:30, "
                        "every 15 minutes, sun 03:00, monthly on the 1st at 9am, "
                        "once tomorrow 9am, at login, or a cron expression)")


# --------------------------------------------------------------- describing


def _clock(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _join(words: list[str]) -> str:
    if len(words) <= 2:
        return " and ".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _days_phrase(weekdays: frozenset[int]) -> str:
    if weekdays == frozenset(range(7)):
        return "every day"
    if weekdays == frozenset({1, 2, 3, 4, 5}):
        return "weekdays"
    if weekdays == frozenset({0, 6}):
        return "weekends"
    ordered = sorted(weekdays, key=lambda d: (d + 6) % 7)   # Monday first
    return _join([DOW_TITLES[d] + "s" for d in ordered])


def describe(schedule: Schedule) -> str:
    if schedule.kind == "login":
        return "at login"
    if schedule.kind == "once":
        moment = datetime.strptime(schedule.at, "%Y-%m-%dT%H:%M")
        return f"once, {moment:%a} {moment:%Y-%m-%d %H:%M}"
    cron = parse_cron(schedule.cron)
    full = lambda values, low, high: values == frozenset(range(low, high + 1))
    every_month = full(cron.months, 1, 12)
    any_day = cron.day_star and cron.weekday_star
    parts = cron.text.split(" ")
    if any_day and every_month:
        if full(cron.hours, 0, 23):
            if full(cron.minutes, 0, 59):
                return "every minute"
            step = re.fullmatch(r"\*/(\d+)", parts[0])
            if step:
                return f"every {step[1]} minutes"
            if len(cron.minutes) == 1:
                minute = next(iter(cron.minutes))
                return "every hour" if minute == 0 else f"every hour at :{minute:02d}"
        step = re.fullmatch(r"\*/(\d+)", parts[1])
        if step and len(cron.minutes) == 1:
            minute = next(iter(cron.minutes))
            return f"every {step[1]} hours" + (f" at :{minute:02d}" if minute else "")
    if len(cron.minutes) == 1 and len(cron.hours) <= 4:
        minute = next(iter(cron.minutes))
        clocks = _join([_clock(h, minute) for h in sorted(cron.hours)])
        if not every_month:
            return f"cron {cron.text}"
        if any_day:
            return f"every day at {clocks}"
        if cron.day_star:            # weekdays only
            return f"{_days_phrase(cron.weekdays)} at {clocks}"
        if cron.weekday_star and len(cron.days) <= 3:
            days = _join([_nth(d) for d in sorted(cron.days)])
            return f"monthly on the {days} at {clocks}"
    return f"cron {cron.text}"


def _nth(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ----------------------------------------------------------- the backends


def _systemd_list(values: frozenset[int], low: int, high: int, width: int = 2) -> str:
    if values == frozenset(range(low, high + 1)):
        return "*"
    ordered = sorted(values)
    runs: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        a, b = ordered[i], ordered[j]
        if j - i >= 2:
            runs.append(f"{a:0{width}d}..{b:0{width}d}")
        else:
            runs.extend(f"{v:0{width}d}" for v in ordered[i:j + 1])
        i = j + 1
    return ",".join(runs)


def _systemd_weekdays(values: frozenset[int]) -> str:
    # systemd weekday ranges run Monday..Sunday, so write a list.
    ordered = sorted(values, key=lambda d: (d + 6) % 7)
    return ",".join(SYSTEMD_DOW[d] for d in ordered)


def to_oncalendar(schedule: Schedule) -> list[str]:
    """The ``OnCalendar=`` values for a timer. Empty for a login schedule."""
    if schedule.kind == "login":
        return []
    if schedule.kind == "once":
        moment = datetime.strptime(schedule.at, "%Y-%m-%dT%H:%M")
        return [moment.strftime("%Y-%m-%d %H:%M:00")]
    cron = parse_cron(schedule.cron)
    clock = (f"{_systemd_list(cron.hours, 0, 23)}:"
             f"{_systemd_list(cron.minutes, 0, 59)}:00")
    month = _systemd_list(cron.months, 1, 12)
    days = _systemd_list(cron.days, 1, 31)
    weekdays = _systemd_weekdays(cron.weekdays)
    all_weekdays = cron.weekdays == frozenset(range(7))
    if not cron.day_star and not cron.weekday_star:
        # Vixie OR: one line per restriction.
        return [f"*-{month}-{days} {clock}", f"{weekdays} *-{month}-* {clock}"]
    prefix = "" if all_weekdays else weekdays + " "
    return [f"{prefix}*-{month}-{days} {clock}"]


def to_crontab(schedule: Schedule) -> str:
    """The time fields of a crontab line."""
    if schedule.kind == "login":
        return "@reboot"
    if schedule.kind == "once":
        # Cron has no years: the line fires yearly and the runner skips the
        # years that are not this one.
        moment = datetime.strptime(schedule.at, "%Y-%m-%dT%H:%M")
        return f"{moment.minute} {moment.hour} {moment.day} {moment.month} *"
    return schedule.cron
