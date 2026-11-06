"""Embeddings for acceptance criteria and commit diffs.

The default embedder is a feature-hashing bag of words (256 dimensions). It needs no
model download, no network and no API key, so the demo runs anywhere and the tests are
deterministic. It is lexical, not semantic: it finds "strip ANSI codes" near a diff
that says "strip_ansi", but not near one that says "remove colour escapes". The
``Embedder`` protocol is the seam for a real model (for example a hosted embedding API)
in production; vectors are stored per (entity, model) so both can coexist.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]+")
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "be",
        "by",
        "with",
        "when",
        "that",
        "this",
        "it",
        "as",
        "at",
        "from",
        "not",
        "no",
        "instead",
        "its",
        "their",
        "any",
        "every",
        "than",
        "then",
        "into",
        "only",
        "also",
        "have",
        "has",
        "does",
    ]
)


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, text: str) -> list[float]: ...


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for raw in TOKEN.findall(text):
        for part in CAMEL.sub(" ", raw).replace("_", " ").split():
            word = part.lower()
            if len(word) > 1 and word not in STOP:
                out.append(word)
    return out


class HashingEmbedder:
    model = "hashing-256-v1"
    dim = 256

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in tokens(text):
            digest = hashlib.blake2b(tok.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def diff_text(message: str, diff: str | None) -> str:
    """What we embed for a commit: its message plus the lines it added."""
    added = [
        line[1:] for line in (diff or "").splitlines() if line.startswith("+") and not line.startswith("+++")
    ]
    return message + "\n" + "\n".join(added[:400])
