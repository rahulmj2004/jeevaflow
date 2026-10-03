"""
Local biomedical sentence encoder.

    NeuML/pubmedbert-base-embeddings (Apache-2.0): PubMedBERT-base
    fine-tuned with sentence-transformers on PubMed title/abstract
    pairs; mean pooling, 768 dimensions.

Inference is a plain NumPy implementation of the BERT encoder read
straight from the pinned safetensors file: no PyTorch, no network, no
model download at runtime. Weights are fetched once by
scripts/fetch_followthrough_model.py and every file is SHA-256 pinned.

Verification cache: hashing 438 MB on every worker job is slow, so a
verified file's (size, mtime, inode) is stamped next to it and the
hash is recomputed whenever that changes. The stamp guards against
corruption and accidental swaps, not against someone with write
access to the model directory (they could rewrite the stamp too).
"""

import hashlib
import json
import math
import struct
from pathlib import Path
from typing import Optional

import numpy as np

MODEL_NAME = "pubmedbert-base-embeddings"
MODEL_REVISION = "b79526d6ef3645e0df4530322e266f24c829f5ef"
MODEL_ID = f"{MODEL_NAME}@{MODEL_REVISION[:7]}"
MODEL_SOURCE = f"https://huggingface.co/NeuML/{MODEL_NAME}/resolve/{MODEL_REVISION}"

# local file -> (path in the source repository, sha256)
PINNED_FILES = {
    "model.safetensors": ("model.safetensors", "929ddc16369bb4ff6f8e92a6ee9f5e0748f2bef33989b5617533dd50beebe4ef"),
    "tokenizer.json": ("tokenizer.json", "6e046044df8a2fcedb10607075dca187cae61d806c0d80a96c5b81017edc90c9"),
    "config.json": ("config.json", "d384b4ca85c7c1eb07767aa69479b722d255f8a40e4a484c6428982b8f18baca"),
}

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / MODEL_NAME
STAMP = ".verified.json"
MAX_TOKENS = 64
DIMENSIONS = 768


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)

    return digest.hexdigest()


def _signature(path: Path) -> list:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino]


def verify(model_dir: Path = MODEL_DIR) -> Optional[str]:
    """
    None when every pinned file is present and matches its hash;
    otherwise a fixed reason code.
    """

    stamp_path = model_dir / STAMP

    try:
        stamp = json.loads(stamp_path.read_text())
    except Exception:
        stamp = {}

    changed = False

    for name, (_, expected) in PINNED_FILES.items():
        path = model_dir / name

        if not path.is_file():
            return "MODEL_MISSING"

        entry = stamp.get(name)

        if entry and entry.get("sha256") == expected and entry.get("signature") == _signature(path):
            continue

        if sha256_file(path) != expected:
            return "MODEL_CHECKSUM_MISMATCH"

        stamp[name] = {"sha256": expected, "signature": _signature(path)}
        changed = True

    if changed:
        try:
            stamp_path.write_text(json.dumps(stamp))
        except OSError:
            pass  # read-only model directory: verify by hash every time

    return None


def _read_safetensors(path: Path) -> dict:
    with path.open("rb") as handle:
        header_size = struct.unpack("<Q", handle.read(8))[0]
        header = json.loads(handle.read(header_size))

    data = np.memmap(path, dtype=np.uint8, mode="r", offset=8 + header_size)
    tensors = {}

    for name, info in header.items():
        if name == "__metadata__":
            continue

        if info["dtype"] != "F32":
            raise ValueError("unsupported tensor dtype")

        start, end = info["data_offsets"]
        tensors[name] = data[start:end].view(np.float32).reshape(info["shape"])

    return tensors


def _erf(x: np.ndarray) -> np.ndarray:
    # Abramowitz & Stegun 7.1.26 (max abs error 1.5e-7).
    sign = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    return sign * (1.0 - poly * np.exp(-x * x))


def _gelu(x: np.ndarray) -> np.ndarray:
    return 0.5 * x * (1.0 + _erf(x / math.sqrt(2.0)))


def _layer_norm(x: np.ndarray, weight: np.ndarray, bias: np.ndarray, eps: float) -> np.ndarray:
    mean = x.mean(-1, keepdims=True)
    variance = ((x - mean) ** 2).mean(-1, keepdims=True)
    return (x - mean) / np.sqrt(variance + eps) * weight + bias


class Encoder:
    def __init__(self, model_dir: Path = MODEL_DIR):
        from tokenizers import Tokenizer

        config = json.loads((model_dir / "config.json").read_text())
        self.layers = config["num_hidden_layers"]
        self.heads = config["num_attention_heads"]
        self.eps = config["layer_norm_eps"]

        self.weights = _read_safetensors(model_dir / "model.safetensors")
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding()

    def _linear(self, x: np.ndarray, name: str) -> np.ndarray:
        return x @ self.weights[f"{name}.weight"].T + self.weights[f"{name}.bias"]

    def encode(self, texts: list[str]) -> np.ndarray:
        """
        L2-normalised mean-pooled embeddings, shape (len(texts), 768).
        """

        if not texts:
            return np.zeros((0, DIMENSIONS), dtype=np.float32)

        w = self.weights
        batch = self.tokenizer.encode_batch(texts)
        ids = np.array([item.ids for item in batch])
        mask = np.array([item.attention_mask for item in batch], dtype=np.float32)
        types = np.array([item.type_ids for item in batch])
        positions = np.arange(ids.shape[1])

        x = (
            w["embeddings.word_embeddings.weight"][ids]
            + w["embeddings.position_embeddings.weight"][positions][None]
            + w["embeddings.token_type_embeddings.weight"][types]
        )
        x = _layer_norm(x, w["embeddings.LayerNorm.weight"], w["embeddings.LayerNorm.bias"], self.eps)

        batch_size, length, hidden = x.shape
        head = hidden // self.heads
        attention_bias = (1.0 - mask)[:, None, None, :] * -1e9

        def split(t):
            return t.reshape(batch_size, length, self.heads, head).transpose(0, 2, 1, 3)

        for index in range(self.layers):
            p = f"encoder.layer.{index}"
            q = split(self._linear(x, f"{p}.attention.self.query"))
            k = split(self._linear(x, f"{p}.attention.self.key"))
            v = split(self._linear(x, f"{p}.attention.self.value"))

            scores = q @ k.transpose(0, 1, 3, 2) / math.sqrt(head) + attention_bias
            scores = np.exp(scores - scores.max(-1, keepdims=True))
            scores /= scores.sum(-1, keepdims=True)

            context = (scores @ v).transpose(0, 2, 1, 3).reshape(batch_size, length, hidden)
            x = _layer_norm(
                self._linear(context, f"{p}.attention.output.dense") + x,
                w[f"{p}.attention.output.LayerNorm.weight"], w[f"{p}.attention.output.LayerNorm.bias"], self.eps,
            )

            inner = _gelu(self._linear(x, f"{p}.intermediate.dense"))
            x = _layer_norm(
                self._linear(inner, f"{p}.output.dense") + x,
                w[f"{p}.output.LayerNorm.weight"], w[f"{p}.output.LayerNorm.bias"], self.eps,
            )

        pooled = (x * mask[..., None]).sum(1) / mask.sum(1, keepdims=True)
        pooled /= np.linalg.norm(pooled, axis=1, keepdims=True)

        return pooled.astype(np.float32)


def load(model_dir: Path = MODEL_DIR) -> tuple[Optional[Encoder], Optional[str]]:
    """
    (encoder, None) or (None, reason). Never raises: without a
    verified model the engine falls back to the existing rules.
    """

    problem = verify(model_dir)

    if problem:
        return None, problem

    try:
        return Encoder(model_dir), None
    except Exception:
        return None, "MODEL_LOAD_FAILED"
