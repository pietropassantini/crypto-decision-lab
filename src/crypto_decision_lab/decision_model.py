"""S05 Decision Model: portable artefact, scoring adapter and Strategy Layer hooks.

Artefact
--------
A trained model is a JSON document (no pickle) holding the feature schema,
the standardization, the logistic coefficients, the selected score threshold
and the provenance of the training data.  Its identity is the SHA-256 of the
canonical JSON *without* the ``model_sha256`` field, recorded inside the file
and re-verified on every load.

Adapter contract (:class:`DecisionModelAdapter`)
-----------------------------------------------
* ``feature_schema`` lists the inference inputs; it never contains a target
  column of the S02 dataset (refused at load time);
* ``score(features)`` returns a probability in ``[0, 1]`` computed only from
  the schema features, or ``None`` when a feature is missing or non-finite
  (the strategy then fails closed: no opening);
* scoring is deterministic and identical to the training pipeline.

Strategy Layer
--------------
Strategies decide only at **decision points** (events carrying
``decision_point``: the S02 cutoffs) and keep the current exposure in between,
so an entry at a cutoff and an exit at the next cutoff fill at the same opens
as the S02 targets.  ``close_only`` marks the final decision point of an
evaluation window: every position is closed there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .dataset_builder import TARGET_COLUMNS
from .decision_contract import ACTION_LONG
from .execution_contract import ACTION_FLAT

MODEL_SCHEMA = "cdl.s05.decision-model.v1"
DECISION_POINT = "decision_point"
CLOSE_ONLY = "close_only"
FORBIDDEN_INPUTS = frozenset(TARGET_COLUMNS) | {"long_trade_worth", "cutoff", "window_index"}


class DecisionModelError(ValueError):
    """Raised for malformed, tampered or leaking model artefacts."""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def artefact_digest(document: Mapping[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "model_sha256"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def check_feature_schema(features: Sequence[str]) -> list[str]:
    schema = [str(name) for name in features]
    if not schema:
        raise DecisionModelError("feature schema must not be empty")
    if len(set(schema)) != len(schema):
        raise DecisionModelError("feature schema contains duplicates")
    leaked = sorted(set(schema) & FORBIDDEN_INPUTS)
    if leaked:
        raise DecisionModelError(f"feature schema contains target or identity columns: {leaked}")
    return schema


def train_logistic(
    X: np.ndarray,
    y: np.ndarray,
    feature_schema: Sequence[str],
    *,
    C: float = 1.0,
    seed: int = 0,
    class_weight: str | None = None,
) -> dict[str, Any]:
    """Fit standardized logistic regression; return the portable parameters."""

    from sklearn.linear_model import LogisticRegression

    schema = check_feature_schema(feature_schema)
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=int)
    if X.ndim != 2 or X.shape[1] != len(schema) or len(X) != len(y):
        raise DecisionModelError("training matrix does not match the feature schema")
    if not np.isfinite(X).all():
        raise DecisionModelError("training features must be finite")
    classes = set(np.unique(y).tolist())
    if classes != {0, 1}:
        raise DecisionModelError(f"training target needs both classes, got {sorted(classes)}")
    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale[scale == 0] = 1.0
    Z = (X - mean) / scale
    model = LogisticRegression(C=float(C), max_iter=5000, random_state=int(seed), class_weight=class_weight)
    model.fit(Z, y)
    return {
        "type": "logistic_regression",
        "feature_schema": schema,
        "mean": [float(v) for v in mean],
        "scale": [float(v) for v in scale],
        "coef": [float(v) for v in model.coef_[0]],
        "intercept": float(model.intercept_[0]),
        "hyperparameters": {"C": float(C), "seed": int(seed), "class_weight": class_weight,
                            "solver": "lbfgs", "standardization": "train mean / std (ddof=0)"},
        "training_rows": int(len(y)),
        "training_positive_rate": float(y.mean()),
    }


def build_artefact(parameters: Mapping[str, Any], *, score_threshold: float, provenance: Mapping[str, Any]) -> dict:
    if not 0.0 <= float(score_threshold) <= 1.0:
        raise DecisionModelError("score_threshold must be a probability")
    document = {
        "schema_version": MODEL_SCHEMA,
        **dict(parameters),
        "score_threshold": float(score_threshold),
        "output": "probability that the LONG window is worth trading (S02 long_trade_worth)",
        "provenance": dict(provenance),
    }
    document["model_sha256"] = artefact_digest(document)
    return document


def write_artefact(document: Mapping[str, Any], path: str | Path) -> Path:
    path = Path(path)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path


@dataclass(frozen=True)
class DecisionModelAdapter:
    document: Mapping[str, Any]
    feature_schema: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        document = self.document
        if document.get("schema_version") != MODEL_SCHEMA:
            raise DecisionModelError(f"unsupported model schema: {document.get('schema_version')}")
        if document.get("model_sha256") != artefact_digest(document):
            raise DecisionModelError("model artefact digest mismatch (tampered or not canonical)")
        schema = check_feature_schema(document["feature_schema"])
        for key in ("mean", "scale", "coef"):
            if len(document[key]) != len(schema):
                raise DecisionModelError(f"model parameter {key} does not match the feature schema")
        object.__setattr__(self, "feature_schema", tuple(schema))

    @classmethod
    def load(cls, path: str | Path, *, expected_sha256: str | None = None) -> "DecisionModelAdapter":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DecisionModelError(f"cannot read model artefact {path}: {exc}") from exc
        adapter = cls(document)
        if expected_sha256 and adapter.model_sha256 != expected_sha256:
            raise DecisionModelError(
                f"model artefact {path} is {adapter.model_sha256}, configuration expects {expected_sha256}"
            )
        return adapter

    @property
    def model_sha256(self) -> str:
        return str(self.document["model_sha256"])

    @property
    def score_threshold(self) -> float:
        return float(self.document["score_threshold"])

    def score(self, features: Mapping[str, float]) -> float | None:
        values = []
        for name in self.feature_schema:
            value = features.get(name)
            if value is None or not math.isfinite(float(value)):
                return None
            values.append(float(value))
        return float(self.score_matrix(np.asarray([values]))[0])

    def score_matrix(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        z = (X - np.asarray(self.document["mean"])) / np.asarray(self.document["scale"])
        logits = z @ np.asarray(self.document["coef"]) + float(self.document["intercept"])
        return 1.0 / (1.0 + np.exp(-logits))


# ------------------------------------------------------------- strategies
def _hold(portfolio, reason: str):
    from .simulation import Proposal

    return Proposal(ACTION_LONG if portfolio.quantity else ACTION_FLAT, reason)


@dataclass(frozen=True)
class ScheduledMomentumStrategy:
    """Baseline: LONG at a decision point when momentum exceeds the threshold."""

    threshold: float = 0.0
    feature: str = "momentum_24h"

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.threshold)):
            raise ValueError("threshold must be finite")

    def signal(self, features: Mapping[str, float]) -> tuple[bool, str]:
        value = features.get(self.feature)
        active = value is not None and value > self.threshold
        return active, f"{self.feature}={value}; threshold={self.threshold}"

    def decide(self, cutoff, features, portfolio):
        from .simulation import Proposal

        if DECISION_POINT not in features:
            return _hold(portfolio, "no decision point: hold current exposure")
        if CLOSE_ONLY in features:
            return Proposal(ACTION_FLAT, "evaluation window closed")
        active, reason = self.signal(features)
        return Proposal(ACTION_LONG if active else ACTION_FLAT, reason)


@dataclass(frozen=True)
class ModelFilteredMomentumStrategy:
    """Variant: the baseline entry also requires score >= score_threshold."""

    model_path: str
    model_sha256: str
    threshold: float = 0.0
    feature: str = "momentum_24h"
    score_threshold: float | None = None
    adapter: DecisionModelAdapter = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        adapter = DecisionModelAdapter.load(self.model_path, expected_sha256=self.model_sha256)
        object.__setattr__(self, "adapter", adapter)
        if self.score_threshold is not None and not 0.0 <= float(self.score_threshold) <= 1.0:
            raise ValueError("score_threshold must be a probability")

    @property
    def effective_threshold(self) -> float:
        return float(self.adapter.score_threshold if self.score_threshold is None else self.score_threshold)

    def decide(self, cutoff, features, portfolio):
        from .simulation import Proposal

        base = ScheduledMomentumStrategy(self.threshold, self.feature)
        if DECISION_POINT not in features:
            return _hold(portfolio, "no decision point: hold current exposure")
        if CLOSE_ONLY in features:
            return Proposal(ACTION_FLAT, "evaluation window closed")
        active, reason = base.signal(features)
        if not active:
            return Proposal(ACTION_FLAT, reason)
        score = self.adapter.score(features)
        if score is None:
            return Proposal(ACTION_FLAT, f"{reason}; score unavailable: fail closed")
        verdict = ACTION_LONG if score >= self.effective_threshold else ACTION_FLAT
        return Proposal(
            verdict,
            f"{reason}; score={score:.6f} {'>=' if verdict == ACTION_LONG else '<'} {self.effective_threshold} "
            f"(model {self.adapter.model_sha256[:12]})",
        )


__all__ = [
    "CLOSE_ONLY",
    "DECISION_POINT",
    "DecisionModelAdapter",
    "DecisionModelError",
    "MODEL_SCHEMA",
    "ModelFilteredMomentumStrategy",
    "ScheduledMomentumStrategy",
    "artefact_digest",
    "build_artefact",
    "check_feature_schema",
    "train_logistic",
    "write_artefact",
]
