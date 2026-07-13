from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[6]
OPS = REPO_ROOT / "ops"


def test_scheduled_wrappers_are_valid_bash_and_gate_mutations_on_clean_boundary() -> None:
    for name in ("nightly-evolve.sh.template", "weekly-epic.sh.template"):
        path = OPS / name
        subprocess.run(["bash", "-n", str(path)], check=True, capture_output=True, text=True)
        text = path.read_text(encoding="utf-8")
        boundary = text.index("--require-clean-boundary")
        assert boundary < text.index("git fetch")
        assert boundary < text.index("git merge --ff-only")
        assert boundary < text.index('/bin/bash -lc "$FLOW_REFRESH_CMD"')
        assert "dirty checkout or live/corrupt run lease" in text
