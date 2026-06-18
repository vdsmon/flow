"""Reference embedder for the semantic-recall seam — run BY `uvx`, not imported.

The ONLY file that imports model2vec/numpy. It is a standalone subprocess
entrypoint: `memory_embed.embed` shells the default command
`uvx --with model2vec[inference] python embedder_model2vec.py --model <id>`, so
model2vec resolves in uvx's own cached env, independent of the runtime python3
(which cannot import numpy). No stdlib-path script imports this module.

Contract:
  stdin  — newline-delimited texts (one per line; trailing newline ignored).
  stdout — a JSON array of vectors, `[[float, ...], ...]`, one per input line.

Exit codes:
  0 = ok.
  1 = model load / encode failure (stderr carries the reason).
"""

from __future__ import annotations

import argparse
import json
import sys

_DEFAULT_MODEL = "minishlab/potion-retrieval-32M"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="model2vec reference embedder (stdin -> JSON).")
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    texts = [line.rstrip("\n") for line in sys.stdin.read().splitlines()]
    try:
        from model2vec import StaticModel

        model = StaticModel.from_pretrained(args.model)
        embeddings = model.encode(texts) if texts else []
    except Exception as exc:  # model load / encode is the whole job; report and fail
        sys.stderr.write(f"embedder_model2vec: {type(exc).__name__}: {exc}\n")
        return 1
    vectors = [list(map(float, row)) for row in embeddings]
    sys.stdout.write(json.dumps(vectors))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
