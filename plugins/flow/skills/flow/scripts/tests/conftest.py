"""Pytest config for plugins/flow/skills/flow/scripts/tests/.

Adds the parent scripts/ dir to sys.path so tests can `import tracker`
without packaging the module.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Replaced, not emptied: commit call sites that set no local user and `git init` call
# sites that omit `-b` both depend on values that used to arrive from ~/.gitconfig.
_HERMETIC_GITCONFIG = """\
[user]
\tname = flow tests
\temail = flow-tests@example.invalid
[init]
\tdefaultBranch = main
"""


@pytest.fixture(scope="session", autouse=True)
def _hermetic_git(tmp_path_factory: pytest.TempPathFactory):
    """Cut every git subprocess in the suite off from developer git config.

    A leaked `core.fsmonitor = true` leaves one detached daemon per throwaway repo long
    after its tmp dir is gone, until the per-user process budget fills and arbitrary
    git spawns start failing. `diff.external` rewrites diffs a test may be parsing.
    """
    config = tmp_path_factory.mktemp("git-home") / "gitconfig"
    config.write_text(_HERMETIC_GITCONFIG, encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GIT_CONFIG_GLOBAL", str(config))
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield
