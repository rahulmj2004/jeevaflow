"""
lexical-fallback backend: OUR ALGORITHM (deterministic, no model).

Used when the pinned biomedical encoder is unavailable (not fetched,
checksum mismatch, or a host without the memory for it). It hashes
character trigrams and whole words into a fixed-size vector and
L2-normalises it, so cosine similarity measures spelling overlap.

It has NO medical knowledge: "thyroid function" and "TSH" share no
characters and score near zero. It is calibrated and evaluated
separately (evaluation/followthrough_eval.py) and every result it
produces is labelled backend = "lexical-fallback".
"""

import re
import zlib

import numpy as np

MODEL_ID = "lexical-fallback@v1"
DIMENSIONS = 2048


def _features(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    features = [f"w:{word}" for word in words]

    for word in words:
        padded = f"#{word}#"
        features += [f"c:{padded[i:i + 3]}" for i in range(len(padded) - 2)]

    return features


class LexicalEncoder:
    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), DIMENSIONS), dtype=np.float32)

        for row, text in enumerate(texts):
            for feature in _features(text):
                # crc32: stable across processes (unlike hash()).
                vectors[row, zlib.crc32(feature.encode()) % DIMENSIONS] += 1.0

        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0

        return vectors / norms
