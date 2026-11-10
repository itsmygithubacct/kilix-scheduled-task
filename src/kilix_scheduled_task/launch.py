"""Make kilix-pane and its stack modules importable from wherever they are.

Order: an ordinary import (a Debian package or a venv); this checkout's
``third_party/`` submodules; sibling checkouts that `kilix_pane.deps`
knows about. The window needs kilix-pane and kitty-input; pixels need
soft-raster as well, and everything else falls back to characters.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIRD_PARTY = ROOT / "third_party"


def prepare() -> None:
    try:
        import kilix_pane  # noqa: F401
    except ImportError:
        candidates = [THIRD_PARTY / "kilix-pane" / "src"]
        home = Path.home()
        for base in filter(None, (os.environ.get("GPU_TERMINAL_HOME"),
                                  str(home / "gpu_terminal"),
                                  str(home / ".local" / "gpu_terminal" / "sources"))):
            candidates.append(Path(base) / "kilix-modules" / "kilix-pane" / "src")
        for candidate in candidates:
            if (candidate / "kilix_pane" / "__init__.py").is_file():
                sys.path.insert(0, str(candidate))
                break
        else:
            return
    from kilix_pane import deps

    deps.add_roots(THIRD_PARTY)
