"""End to end on the real systemd user manager. Opt-in: it creates real units.

    make e2e

Units get a unique prefix (kst-e2e-<random>-) and are removed at the end,
pass or fail; the task file, history and runner live in a temporary
directory. It checks that `run` starts the service, that a timer fires the
runner on time, that the runs are recorded, and that removal leaves no unit
behind. It takes one to two minutes, because a calendar timer fires on a
whole minute.
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("KST_E2E") == "1", "set KST_E2E=1 (make e2e)")
class RealSystemd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.prefix = f"kst-e2e-{secrets.token_hex(3)}-"
        self.env = dict(os.environ, NO_COLOR="1",
                        KILIX_SCHEDULED_TASK_FILE=str(root / "tasks.json"),
                        XDG_STATE_HOME=str(root / "state"), XDG_DATA_HOME=str(root / "data"),
                        KILIX_SCHEDULED_TASK_BACKEND="systemd",
                        KILIX_SCHEDULED_TASK_PREFIX=self.prefix,
                        PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        self.state = root / "state" / "kilix-scheduled-task"
        self.units = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"

    def tearDown(self):
        try:
            for job in json.loads(self.cli("list", "--json").stdout or "[]"):
                self.cli("rm", "-y", job["id"], check=False)
        finally:
            leftovers = sorted(p.name for p in self.units.glob(self.prefix + "*"))
            for path in self.units.glob(self.prefix + "*"):
                subprocess.run(["systemctl", "--user", "disable", "--now", path.name],
                               capture_output=True)
                path.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            self.dir.cleanup()
            self.assertEqual(leftovers, [], "rm left units behind")

    def cli(self, *args, check=True):
        result = subprocess.run([sys.executable, str(ROOT / "kilix-scheduled-task"), *args],
                                capture_output=True, text=True, env=self.env, timeout=120)
        if check and result.returncode != 0:
            self.fail(f"{args}: {result.returncode}\n{result.stdout}\n{result.stderr}")
        return result

    def runs(self, job_id):
        path = self.state / job_id / "runs.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, job_id, count, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.runs(job_id)) >= count:
                return self.runs(job_id)
            time.sleep(1)
        self.fail(f"{job_id}: {len(self.runs(job_id))} run(s) after {timeout} s")

    def test_service_and_timer(self):
        self.cli("add", "E2E now", "--at", "hourly", "--", "echo", "from-the-service")
        self.cli("run", "e2e-now")
        record = self.wait_for("e2e-now", 1, 30)[0]
        self.assertEqual((record["status"], record["trigger"]), ("ok", "manual"))
        self.assertIn("from-the-service", self.cli("logs", "e2e-now").stdout)

        self.cli("add", "E2E timer", "--at", "in 1 minute", "--", "echo", "fired-by-the-timer")
        timers = subprocess.run(["systemctl", "--user", "list-timers", "--all", self.prefix + "*"],
                                capture_output=True, text=True).stdout
        self.assertIn(self.prefix + "e2e-timer.timer", timers)
        record = self.wait_for("e2e-timer", 1, 150)[0]
        self.assertEqual((record["status"], record["trigger"]), ("ok", "schedule"))
        self.assertIn("fired-by-the-timer", self.cli("logs", "e2e-timer").stdout)
        self.assertEqual(self.cli("doctor").returncode, 0, self.cli("doctor", check=False).stdout)


if __name__ == "__main__":
    unittest.main()
