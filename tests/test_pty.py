"""The window on a real pseudo-terminal, started by the launcher."""

import fcntl
import json
import os
import pty
import select
import signal
import struct
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_until(fd, needle, timeout=15.0):
    out = b""
    deadline = time.monotonic() + timeout
    while needle not in out and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    return out


def wait(pid, fd, timeout=15.0):
    out = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                out += os.read(fd, 65536)
            except OSError:
                pass
        if done:
            return out, status
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    raise AssertionError("kilix-scheduled-task did not exit")


class RealTerminal(unittest.TestCase):
    def test_create_a_task_then_quit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = dict(os.environ, TERM="xterm-256color",
                       XDG_CONFIG_HOME=str(root / "config"), XDG_STATE_HOME=str(root / "state"),
                       XDG_DATA_HOME=str(root / "data"), KILIX_SCHEDULED_TASK_BACKEND="none",
                       PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
            for key in ("KITTY_WINDOW_ID", "KILIX_STREAM", "TMUX", "TERM_PROGRAM",
                        "KILIX_SCHEDULED_TASK_FILE"):
                env.pop(key, None)
            pid, fd = pty.fork()
            if pid == 0:
                os.execvpe(sys.executable, [sys.executable, str(ROOT / "kilix-scheduled-task")], env)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
            try:
                start = read_until(fd, b"No scheduled tasks yet")
                self.assertIn(b"\x1b[?1049h", start)
                for chunk in (b"n", "Café ☕\t".encode(), b"weekdays 7am\t", b"echo hi",
                              b"\x13", b"\r"):
                    os.write(fd, chunk)
                    time.sleep(0.2)
                read_until(fd, "Café".encode())
                os.write(fd, b"q")
                rest, status = wait(pid, fd)
            finally:
                os.close(fd)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"\x1b[?1049l", rest)
            data = json.loads((root / "config" / "kilix-scheduled-task" / "tasks.json").read_text())
            self.assertEqual(data["jobs"][0]["name"], "Café ☕")
            self.assertEqual(data["jobs"][0]["schedule"], {"kind": "cron", "cron": "0 7 * * 1-5"})


if __name__ == "__main__":
    unittest.main()
