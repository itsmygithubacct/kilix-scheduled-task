"""The command line in a subprocess, with fake systemctl and loginctl on PATH.

The runner is real: `run --here` executes the command and records it.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$2" in
  show-environment) echo HOME=/x ;;
  show) printf 'ActiveState=active\\nUnitFileState=enabled\\n' ;;
esac
exit 0
"""


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        fakebin = root / "bin"
        fakebin.mkdir()
        (fakebin / "systemctl").write_text(FAKE_SYSTEMCTL)
        (fakebin / "loginctl").write_text("#!/bin/sh\necho yes\n")
        for tool in fakebin.iterdir():
            tool.chmod(0o755)
        self.log = root / "systemctl.log"
        self.env = dict(os.environ, NO_COLOR="1", FAKE_LOG=str(self.log),
                        PATH=f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
                        XDG_CONFIG_HOME=str(root / "config"),
                        XDG_STATE_HOME=str(root / "state"),
                        XDG_DATA_HOME=str(root / "data"),
                        KILIX_SCHEDULED_TASK_BACKEND="systemd",
                        PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        self.env.pop("KILIX_SCHEDULED_TASK_FILE", None)
        self.env.pop("KILIX_SCHEDULED_TASK_PREFIX", None)
        self.units = root / "config" / "systemd" / "user"

    def tearDown(self):
        self.dir.cleanup()

    def cli(self, *args, check=True):
        result = subprocess.run([sys.executable, str(ROOT / "kilix-scheduled-task"), *args],
                                capture_output=True, text=True, env=self.env, timeout=60)
        if check and result.returncode != 0:
            self.fail(f"{args} exited {result.returncode}:\n{result.stdout}\n{result.stderr}")
        return result

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_lifecycle(self):
        out = self.cli("add", "Say hello", "--at", "weekdays 8:30", "--",
                       "echo", "hello world", ";", "echo", "$HOME").stdout
        self.assertIn("say-hello", out)
        self.assertIn("weekdays at 08:30", out)
        job = json.loads(self.cli("show", "say-hello", "--json").stdout)
        self.assertEqual(job["command"], "echo 'hello world' ';' echo '$HOME'")
        self.assertTrue((self.units / "kilix-task-say-hello.timer").exists())
        self.assertIn("--user enable --now kilix-task-say-hello.timer", self.calls())
        self.assertIn("say-hello", self.cli("list").stdout)

        here = self.cli("run", "say-hello", "--here").stdout
        self.assertIn("hello world ; echo $HOME", here)
        self.assertIn("✓", self.cli("history", "say-hello").stdout)
        self.assertIn("hello world", self.cli("logs", "say-hello").stdout)

        self.cli("edit", "say-hello", "--at", "sun 03:00", "-c", "printf 'a b\\n'")
        timer = (self.units / "kilix-task-say-hello.timer").read_text()
        self.assertIn("OnCalendar=Sun *-*-* 03:00:00", timer)
        self.assertIn("--user restart kilix-task-say-hello.timer", self.calls())
        self.assertEqual(json.loads(self.cli("show", "say-hello", "--json").stdout)["command"],
                         "printf 'a b\\n'")

        self.cli("disable", "say-hello")
        self.assertIn("--user disable --now kilix-task-say-hello.timer", self.calls())
        self.assertIn("disabled", self.cli("list").stdout)
        self.cli("enable", "say")          # a unique prefix is enough
        self.assertIn("Unit", self.cli("units", "say-hello").stdout)
        self.assertEqual(self.cli("doctor").returncode, 0)

        refused = self.cli("rm", "say-hello", check=False)
        self.assertEqual(refused.returncode, 2)
        self.cli("rm", "-y", "say-hello")
        self.assertFalse((self.units / "kilix-task-say-hello.timer").exists())
        self.assertIn("no scheduled tasks", self.cli("list").stdout)

    def test_next_previews_without_saving(self):
        out = self.cli("next", "every", "15", "minutes", "-n", "2").stdout
        self.assertIn("every 15 minutes", out)
        self.assertIn("cron: */15 * * * *", out)
        self.assertFalse((Path(self.dir.name) / "config" / "kilix-scheduled-task").exists())

    def test_errors(self):
        bad = self.cli("add", "X", "--at", "whenever", "--", "true", check=False)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("can't read 'whenever'", bad.stderr)
        bad = self.cli("add", "X", "--at", "daily", check=False)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("after --", bad.stderr)
        bad = self.cli("show", "nothing", check=False)
        self.assertEqual(bad.stderr.strip(), "kilix-scheduled-task: no task 'nothing'")

    def test_no_backend_saves_but_says_so(self):
        self.env["KILIX_SCHEDULED_TASK_BACKEND"] = "none"
        result = self.cli("add", "Later", "--at", "hourly", "--", "true", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing here can run it", result.stderr)
        self.assertIn("later", self.cli("list").stdout)


if __name__ == "__main__":
    unittest.main()
