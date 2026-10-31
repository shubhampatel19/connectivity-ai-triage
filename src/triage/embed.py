"""Text embeddings for runbook search.

fastembed runs a small ONNX model on the CPU (BAAI/bge-small-en-v1.5, 384
dimensions): no API key, no GPU, and the image bakes the model in so nothing
is downloaded at runtime. HashEmbedder is a deterministic stand-in for tests,
which works like a bag of words and needs no model at all.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import re
from typing import Protocol

TOKEN = re.compile(r"[a-z0-9]+(?:[._][a-z0-9]+)*")


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Feature hashing over words and word pairs, L2-normalized."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.name = f"hash-{dim}"

    def _vec(self, text: str) -> list[float]:
        words = TOKEN.findall(text.lower())
        feats = words + [f"{a} {b}" for a, b in itertools.pairwise(words)]
        v = [0.0] * self.dim
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class FastEmbedder:
    def __init__(self, model: str) -> None:
        from fastembed import TextEmbedding

        # FASTEMBED_CACHE_PATH points at the model baked into the image.
        self._model = TextEmbedding(model_name=model, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH"))
        self.name = f"fastembed:{model}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        # bge models embed queries with an instruction prefix; query_embed adds it.
        return next(iter(self._model.query_embed([text]))).tolist()


def make(kind: str, model: str) -> Embedder:
    if kind == "hash":
        return HashEmbedder()
    if kind == "fastembed":
        return FastEmbedder(model)
    raise SystemExit(f"EMBEDDER must be fastembed or hash, not {kind!r}")
