"""Pytest config for plugins/flow/skills/flow/scripts/tests/.

Adds the parent scripts/ dir to sys.path so tests can `import tracker`
without packaging the module.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
