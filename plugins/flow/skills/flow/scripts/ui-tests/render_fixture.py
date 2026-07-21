from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

UI_ROOT = Path(__file__).resolve().parent
SCRIPTS_ROOT = UI_ROOT.parent
sys.path.insert(0, str(SCRIPTS_ROOT))

import review_brief as rb  # noqa: E402

SHA = "a" * 40
SOURCE = (
    "def resolve_scope(cwd):\n"
    "    # Keep <script>alert('no')</script> inert.\n"
    '    return {"root": cwd, "attempts": 3}\n'
)
DIFF = """diff --git a/src/scope.py b/src/scope.py
index 1111111..2222222 100644
--- a/src/scope.py
+++ b/src/scope.py
@@ -1,3 +1,3 @@
 def resolve_scope(cwd):
-    return {"root": cwd}
+    return {"root": cwd, "attempts": 3}
     # Keep <script>alert('no')</script> inert.
"""


class FixtureForge:
    backend = "github"
    capabilities: ClassVar[list[dict[str, object]]] = []

    def pr_info(self, pr_id: str):
        return {
            "id": pr_id,
            "number": int(pr_id),
            "url": f"https://github.com/acme/flow/pull/{pr_id}",
            "draft": False,
            "base": "main",
            "head": "feat/review-brief",
            "head_sha": SHA,
            "state": "OPEN",
        }

    def source_url(self, pr_id: str, sha: str, path: str, start_line: int, end_line: int):
        del pr_id
        return f"https://github.com/acme/flow/blob/{sha}/{path}#L{start_line}-L{end_line}"


def runner(args: list[str]):
    if args == ["git", "rev-parse", "HEAD"]:
        return subprocess.CompletedProcess(args, 0, SHA + "\n", "")
    if args[:2] == ["git", "merge-base"]:
        return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n", "")
    if args[:2] == ["git", "diff"]:
        return subprocess.CompletedProcess(args, 0, DIFF, "")
    if args[:2] == ["git", "show"]:
        return subprocess.CompletedProcess(args, 0, SOURCE, "")
    return subprocess.CompletedProcess(args, 1, "", f"unexpected command: {args}")


def render_fixture(name: str) -> str:
    output_root = UI_ROOT / ".generated" / name
    receipt = rb.render(
        rb.RenderRequest(
            workspace_root=UI_ROOT,
            ticket_dir=output_root,
            pr_id="42",
            content_path=UI_ROOT / "fixtures" / f"{name}.json",
            open_browser=False,
        ),
        forge=FixtureForge(),
        runner=runner,
    )
    return receipt.html_path


if __name__ == "__main__":
    print(json.dumps({name: render_fixture(name) for name in ("full", "compact", "portuguese")}))
