"""Reference embedder for the semantic-recall seam, run BY `uvx`, not imported.

The shipped default embedder. Imports fastembed (ONNX runtime, no torch). It is a
standalone subprocess entrypoint: `memory_embed.embed` shells the default command
`uvx --with fastembed python embedder_fastembed.py --model <id>`, so fastembed
resolves in uvx's own cached env, independent of the runtime python3 (which cannot
import it). No stdlib-path script imports this module. `embedder_model2vec.py` is
the lighter static alternative, selectable via `[memory.semantic].embedder`.

Contract:
  stdin:  newline-delimited texts (one per line; trailing newline ignored).
  stdout: a JSON array of vectors, `[[float, ...], ...]`, one per input line.

Exit codes:
  0 = ok.
  1 = model load / encode failure (stderr carries the reason).
"""

from __future__ import annotations

import argparse
import json
import sys

_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fastembed reference embedder (stdin -> JSON).")
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    texts = [line.rstrip("\n") for line in sys.stdin.read().splitlines()]
    if not texts:
        sys.stdout.write("[]\n")
        return 0
    try:
        from fastembed import TextEmbedding

        model = TextEmbedding(model_name=args.model)
        embeddings = list(model.embed(texts))
    except Exception as exc:  # model load / encode is the whole job; report and fail
        sys.stderr.write(f"embedder_fastembed: {type(exc).__name__}: {exc}\n")
        return 1
    vectors = [list(map(float, row)) for row in embeddings]
    sys.stdout.write(json.dumps(vectors))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
