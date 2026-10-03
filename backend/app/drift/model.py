"""
Local natural-language-inference model (premise -> hypothesis:
contradiction / entailment / neutral), used only to test whether an
extracted statement conflicts with its source quote.

    cross-encoder nli-deberta-v3-xsmall (Apache-2.0), int8-quantised
    ONNX export, loaded from disk only. Fetch once with
    scripts/fetch_drift_model.py; every file is checksum-pinned and a
    mismatch disables the model (the rule checks still run).

Model scores rank how strongly the source contradicts an extraction.
They are not a measure of clinical correctness.
"""

import hashlib
from pathlib import Path
from typing import Optional

MODEL_NAME = "nli-deberta-v3-xsmall"
MODEL_REVISION = "2a4f614a701367a02d51389039afc998faeda637"
MODEL_ID = f"{MODEL_NAME}@{MODEL_REVISION[:7]}"
MODEL_SOURCE = f"https://huggingface.co/Xenova/{MODEL_NAME}/resolve/{MODEL_REVISION}"

# local file -> (path in the source repository, sha256)
PINNED_FILES = {
    "model_quantized.onnx": (
        "onnx/model_quantized.onnx",
        "3fac2500c45c75af42c7711de0d1b93d59577456100208be0dc1f9e8811946b6",
    ),
    "tokenizer.json": ("tokenizer.json", "a86f883318afa11c8c10466f1bf4efaeb6ded28a52cbe57217a8fa0d0a2a87df"),
    "config.json": ("config.json", "ec0bd14cc28640326474399cd61d38ccd52b64900228799d0f81debda8c4bc53"),
}

LABELS = ("contradiction", "entailment", "neutral")
MAX_TOKENS = 256

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / MODEL_NAME


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)

    return digest.hexdigest()


def verify(model_dir: Path = MODEL_DIR) -> Optional[str]:
    """
    None when every pinned file is present and matches; otherwise a
    fixed reason code.
    """

    for name, (_, expected) in PINNED_FILES.items():
        path = model_dir / name

        if not path.is_file():
            return "MODEL_MISSING"

        if sha256_file(path) != expected:
            return "MODEL_CHECKSUM_MISMATCH"

    return None


class NliModel:
    def __init__(self, model_dir: Path = MODEL_DIR):
        import onnxruntime
        from tokenizers import Tokenizer

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1

        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.session = onnxruntime.InferenceSession(
            str(model_dir / "model_quantized.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.inputs = {item.name for item in self.session.get_inputs()}

    def predict(self, premise: str, hypothesis: str) -> dict:
        import numpy as np

        encoded = self.tokenizer.encode(premise, hypothesis)
        feed = {
            "input_ids": np.array([encoded.ids], dtype=np.int64),
            "attention_mask": np.array([encoded.attention_mask], dtype=np.int64),
        }

        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.array([encoded.type_ids], dtype=np.int64)

        logits = self.session.run(None, feed)[0][0]
        probabilities = np.exp(logits - logits.max())
        probabilities /= probabilities.sum()

        return {label: round(float(p), 4) for label, p in zip(LABELS, probabilities)}


def load(model_dir: Path = MODEL_DIR) -> tuple[Optional[NliModel], Optional[str]]:
    """
    (model, None) or (None, reason). Never raises: without a verified
    model the detector falls back to rule checks and says so.
    """

    problem = verify(model_dir)

    if problem:
        return None, problem

    try:
        return NliModel(model_dir), None
    except Exception:
        return None, "MODEL_LOAD_FAILED"
