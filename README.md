# kilix-scheduled-task

Commands on a schedule, for people who would rather not write unit files or
crontab lines by hand. Say *weekdays 8:30* or *every 15 minutes*; it becomes
a systemd user timer (or a line in your crontab where there is no systemd user
session), and every run is recorded with its output.

In a Kilix or Kitty pane it is a window drawn with pixels beneath the
terminal's own text; anywhere else the same window draws with characters; and
there is a command line for scripts.

```sh
kilix-scheduled-task                     # the window
kilix-scheduled-task add "Todo digest" --at "mon..fri 08:30" -- kilix-todo due --notify
kilix-scheduled-task next "every 2 hours"
kilix-scheduled-task list
kilix-scheduled-task logs todo-digest
```

Python 3.11 or newer, standard library only. The window uses
[kilix-pane](https://github.com/itsmygithubacct/kilix-pane), pinned in
`third_party/`.

## Saying when

| write | becomes |
|---|---|
| `08:30`, `at 9am`, `every day at 9am and 5pm` | every day |
| `weekdays 8:30`, `weekends 10am` | Monday–Friday, Saturday and Sunday |
| `mon,wed,fri 18:00`, `monday and thursday 7pm`, `sat..sun 10am`, `sun 03:00` | those days |
| `every 15 minutes`, `every 2 hours`, `hourly`, `every minute` | intervals that divide the hour or the day |
| `daily`, `weekly`, `monthly`, `yearly` | at midnight (weekly: Sunday; monthly: the 1st) |
| `monthly on the 1st at 9am`, `on the 15th at 10:00` | a day of the month |
| `once tomorrow 9am`, `once 2026-12-24 18:00`, `once fri 7am`, `in 10 minutes` | one run |
| `at login` | when your session starts (cron: at boot) |
| `*/5 9-17 * * 1-5`, `@daily` | cron, as written |

Everything except *once* and *at login* is stored as a five-field cron
expression, so any schedule can be written as cron when words are not enough.
When both the day-of-month and the day-of-week are restricted (`0 9 1 * mon`)
a day matching either runs, as in cron; the systemd timer gets one
`OnCalendar=` line for each, which systemd ORs. The window and `next` show the
next runs before anything is saved; the suite checks the next-run calculation
and the systemd translation against `systemd-analyze calendar` on random
expressions.

## The window

Tasks on the left with a status dot (green: last run ok, red: failed, amber:
running, grey: not run yet or paused) and a countdown to the next run. On the
right: the command, the schedule in words with the next three runs, the run
history, and the selected run's output, which updates while a run is going.

| keys | |
|---|---|
| `n` | new task: name, when (read back as you type), command, directory, time limit, catch-up, notification |
| `e` `enter` | edit |
| `space` | pause or resume |
| `r` | run now, as the schedule would (asks first) |
| `d` | delete, with its history (asks first) |
| `tab` `l` `h` | between the tasks and their runs |
| `enter` on a run | its full output in `$PAGER` |
| `J` `K` `page up/down` | scroll the output |
| `u` | the generated units or crontab line |
| `y` | copy the command |
| `s` | sync the backend again |
| `?` `q` | help, quit |

A new task is scheduled only after a confirmation that shows exactly what will
run, where, when, and through which backend.

## The command line

    kilix-scheduled-task add NAME --at WHEN [--dir D] [--timeout S] [--no-catch-up]
                             [--notify-failure] [--disabled] -- COMMAND…
    kilix-scheduled-task add NAME --at WHEN --command 'SHELL COMMAND'
    kilix-scheduled-task list [--json]  |  show TASK [--json]  |  history TASK [-n N]
    kilix-scheduled-task logs TASK [--run N]
    kilix-scheduled-task run TASK [--here]
    kilix-scheduled-task enable TASK…  |  disable TASK…
    kilix-scheduled-task edit TASK [--name …] [--at …] [--dir …] [--timeout …]
                              [--[no-]catch-up] [--[no-]notify-failure] [-- COMMAND…]
    kilix-scheduled-task rm TASK… [-y] [--keep-history]
    kilix-scheduled-task next WHEN [-n N]
    kilix-scheduled-task units TASK  |  sync  |  doctor  |  gui [--cells|--pixels]

A task is named by its id, its name, or an unambiguous id prefix. Words after
`--` are joined with shell quoting; a single word is taken as a whole shell
command, so `-- 'tar czf a.tgz ~/notes && echo done'` works as you mean it.
Commands run with `/bin/sh -c` in the task's directory (`~` unless you say).

`run` starts the task through its backend, in the same environment the
schedule uses; `run --here` runs it in this terminal and prints the output.
`doctor` compares every timer's state with its definition; `sync` repairs any
difference.

## How it works

The definitions are one JSON file, `$XDG_CONFIG_HOME/kilix-scheduled-task/
tasks.json` (`"schema": "kilix.scheduled-task/v1"`), changed under a lock
and replaced atomically. Every change is followed by a *sync* that makes the
system match the file:

- **systemd**: for each task, `kilix-task-<id>.timer` and `.service` in
  `~/.config/systemd/user/`, then `daemon-reload`, `enable --now` or
  `disable --now`, and a restart of a timer whose schedule changed. *Catch up*
  is `Persistent=true`: a run missed while the computer was off happens once
  at the next start. Only files carrying the `X-KilixScheduledTask=` marker
  are ever written or removed; a file of the same name without it is reported
  and left alone.
- **cron**: one block between `# BEGIN kilix-scheduled-task` and
  `# END kilix-scheduled-task` in your crontab, replaced in place; every other
  line is kept as it was. A paused task stays in the block as a comment. Cron
  has no years, so a *once* line fires on that date every year and the runner
  ignores every year but the right one.

Both call the runner, `kst-run.py`, which is copied to
`$XDG_DATA_HOME/kilix-scheduled-task/` so schedules keep working wherever the
application lives. It runs the command with `~/.local/bin` on `PATH` and the
session bus set (so `notify-send` works from cron), captures up to 1 MiB of
output into `$XDG_STATE_HOME/kilix-scheduled-task/<id>/logs/`, appends a
record to `runs.jsonl` (start, end, duration, exit status, ok / failed /
timeout / stopped / skipped, manual or scheduled), keeps the newest 20 logs
and 500 records, stops the whole process group at the time limit, records a
run that finds the previous one still going as skipped, and can send a desktop
notification on failure. Its exit status is the command's, so
`systemctl --user status kilix-task-<id>` shows failures too.

systemd user timers run while you are logged in. To keep them running after
you log out, `loginctl enable-linger`; `doctor` and the window say when it is
off.

`$KILIX_SCHEDULED_TASK_BACKEND` (`systemd`, `cron`, `none`) overrides the
choice of backend; `$KILIX_SCHEDULED_TASK_FILE` the definitions file.

## Development

```sh
make test   # schedules (against systemd-analyze), backends with a fake system,
            # the real runner, the CLI, the window
make e2e    # the real systemd user manager: units under a random prefix,
            # removed afterwards; one to two minutes
make run    # the window on scratch files, scheduling nothing
```

## License

MIT. See [LICENSE](LICENSE).
