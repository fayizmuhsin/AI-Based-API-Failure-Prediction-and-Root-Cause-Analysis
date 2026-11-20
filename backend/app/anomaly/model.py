"""Standard-library Isolation Forest training, scoring, and safe persistence."""

from __future__ import annotations

import json
import math
import random
import statistics
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.anomaly.features import FEATURE_NAMES
from app.core.config import Settings


MODEL_FORMAT = "pure-python-isolation-forest"
MODEL_FORMAT_VERSION = 1


class IsolationForestModel:
    """Isolation Forest whose score is the standard [0, 1] anomaly score."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pipeline: dict[str, Any] | None = None  # retained as the existing readiness handle
        self.metadata: dict[str, Any] | None = None

    @property
    def ready(self) -> bool:
        return self.pipeline is not None

    def fit(self, rows: list[dict[str, float | None]]) -> tuple[bytes, dict[str, Any]]:
        if not isinstance(rows, list):
            raise ValueError("Training observations must be a list")
        if len(rows) < self.settings.anomaly_min_training_samples:
            raise ValueError(
                f"Need at least {self.settings.anomaly_min_training_samples} healthy windows; got {len(rows)}"
            )
        vectors = [self._vector(row) for row in rows]
        medians = self._medians(vectors)
        vectors = [self._impute(row, medians) for row in vectors]

        sample_size = min(self.settings.anomaly_max_samples, len(vectors))
        max_height = math.ceil(math.log2(sample_size)) if sample_size > 1 else 0
        rng = random.Random(self.settings.anomaly_random_state)
        trees: list[dict[str, Any]] = []
        for _ in range(self.settings.anomaly_n_estimators):
            sample = rng.sample(vectors, sample_size)
            tree_rng = random.Random(rng.getrandbits(64))
            trees.append(self._build_tree(sample, 0, max_height, tree_rng))

        self.pipeline = {
            "medians": medians,
            "trees": trees,
            "sample_size": sample_size,
            "max_height": max_height,
        }
        scores = [self._score_vector(row) for row in vectors]
        threshold = self._quantile(scores, 1.0 - self.settings.anomaly_contamination)
        if min(scores) == max(scores):
            # Avoid classifying every observation anomalous when the training
            # forest cannot distinguish any sample (for example all features
            # are constant).
            threshold = math.nextafter(threshold, math.inf)
        self.pipeline["decision_threshold"] = threshold

        config = {
            "algorithm": "IsolationForest",
            "implementation": MODEL_FORMAT,
            "model_format_version": MODEL_FORMAT_VERSION,
            "contamination": self.settings.anomaly_contamination,
            "random_state": self.settings.anomaly_random_state,
            "n_estimators": self.settings.anomaly_n_estimators,
            "max_samples": sample_size,
            "configured_max_samples": self.settings.anomaly_max_samples,
            "max_tree_height": max_height,
            "preprocessing": "per-feature median imputation; no scaling required for uniform random splits",
            "decision_threshold": threshold,
        }
        metadata = {
            "model_version": str(uuid4()),
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "feature_names": list(FEATURE_NAMES),
            "training_sample_count": len(vectors),
            "model_config": config,
        }
        self.metadata = metadata
        payload = {
            "format": MODEL_FORMAT,
            "format_version": MODEL_FORMAT_VERSION,
            "feature_names": list(FEATURE_NAMES),
            "medians": medians,
            "trees": trees,
            "sample_size": sample_size,
            "max_height": max_height,
            "decision_threshold": threshold,
        }
        return json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8"), metadata

    def activate(self, model_bytes: bytes, metadata: dict[str, Any]) -> None:
        """Load only bounded, schema-checked JSON; never unpickle model data."""
        if not isinstance(model_bytes, bytes) or len(model_bytes) > 20_000_000:
            raise ValueError("Stored model payload is invalid or too large")
        try:
            data = json.loads(model_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Stored model is not valid JSON") from exc
        if not isinstance(data, dict) or data.get("format") != MODEL_FORMAT:
            raise ValueError("Stored model format is unsupported")
        if data.get("format_version") != MODEL_FORMAT_VERSION:
            raise ValueError("Stored model version is unsupported")
        if data.get("feature_names") != list(FEATURE_NAMES):
            raise ValueError("Stored model feature schema does not match this application")
        medians = data.get("medians")
        trees = data.get("trees")
        sample_size = data.get("sample_size")
        max_height = data.get("max_height")
        threshold = data.get("decision_threshold")
        if (
            not isinstance(medians, list) or len(medians) != len(FEATURE_NAMES)
            or any(not self._is_finite_number(value) for value in medians)
            or not isinstance(trees, list) or not trees or len(trees) > 2000
            or not isinstance(sample_size, int) or sample_size < 2
            or not isinstance(max_height, int) or max_height < 1 or max_height > 32
            or not self._is_finite_number(threshold) or not 0.0 <= threshold <= 1.0
            or not isinstance(metadata, dict)
            or metadata.get("feature_names") != list(FEATURE_NAMES)
        ):
            raise ValueError("Stored model has invalid parameters")
        for tree in trees:
            self._validate_node(tree, 0, max_height)
        self.pipeline = {
            "medians": medians,
            "trees": trees,
            "sample_size": sample_size,
            "max_height": max_height,
            "decision_threshold": float(threshold),
        }
        self.metadata = metadata

    def score(self, values: dict[str, float | None]) -> dict[str, Any]:
        if not self.ready:
            return {"score": None, "decision": "not_ready"}
        vector = self._impute(self._vector(values), self.pipeline["medians"])
        score = self._score_vector(vector)
        return {
            "score": score,
            "decision": "anomaly" if score >= self.pipeline["decision_threshold"] else "normal",
        }

    def _score_vector(self, vector: list[float]) -> float:
        forest = self.pipeline
        if forest is None:
            raise RuntimeError("Model has not been trained")
        average_path = sum(
            self._path_length(vector, tree) for tree in forest["trees"]
        ) / len(forest["trees"])
        correction = self.expected_path_length(forest["sample_size"])
        if correction <= 0:
            return 0.5
        return 2.0 ** (-average_path / correction)

    @staticmethod
    def _build_tree(
        rows: list[list[float]], depth: int, max_height: int, rng: random.Random
    ) -> dict[str, Any]:
        if len(rows) <= 1 or depth >= max_height:
            return {"size": len(rows)}
        varying = [
            index for index in range(len(FEATURE_NAMES))
            if min(row[index] for row in rows) < max(row[index] for row in rows)
        ]
        if not varying:
            return {"size": len(rows)}
        feature = rng.choice(varying)
        low = min(row[feature] for row in rows)
        high = max(row[feature] for row in rows)
        split = rng.uniform(low, high)
        left = [row for row in rows if row[feature] < split]
        right = [row for row in rows if row[feature] >= split]
        # A degenerate random split is repaired by choosing a random gap between
        # distinct observed values, still strictly within the feature range.
        if not left or not right:
            distinct = sorted({row[feature] for row in rows})
            gap = rng.randrange(len(distinct) - 1)
            split = (distinct[gap] + distinct[gap + 1]) / 2.0
            left = [row for row in rows if row[feature] < split]
            right = [row for row in rows if row[feature] >= split]
        return {
            "feature": feature,
            "split": split,
            "left": IsolationForestModel._build_tree(left, depth + 1, max_height, rng),
            "right": IsolationForestModel._build_tree(right, depth + 1, max_height, rng),
        }

    @staticmethod
    def _path_length(vector: list[float], node: dict[str, Any], depth: int = 0) -> float:
        if "size" in node:
            return depth + IsolationForestModel.expected_path_length(node["size"])
        branch = "left" if vector[node["feature"]] < node["split"] else "right"
        return IsolationForestModel._path_length(vector, node[branch], depth + 1)

    @staticmethod
    def expected_path_length(size: int) -> float:
        """Expected unsuccessful BST search length c(n), with standard small-n cases."""
        if size <= 1:
            return 0.0
        if size == 2:
            return 1.0
        harmonic = math.fsum(1.0 / value for value in range(1, size))
        return 2.0 * harmonic - (2.0 * (size - 1) / size)

    @staticmethod
    def _vector(row: dict[str, float | None]) -> list[float | None]:
        if not isinstance(row, dict):
            raise ValueError("Each observation must be a feature mapping")
        vector: list[float | None] = []
        for name in FEATURE_NAMES:
            value = row.get(name)
            if value is None:
                vector.append(None)
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Feature {name} must be a finite number or None")
            number = float(value)
            vector.append(number if math.isfinite(number) else None)
        return vector

    @staticmethod
    def _medians(rows: list[list[float | None]]) -> list[float]:
        medians = []
        for index in range(len(FEATURE_NAMES)):
            observed = [row[index] for row in rows if row[index] is not None]
            medians.append(float(statistics.median(observed)) if observed else 0.0)
        return medians

    @staticmethod
    def _impute(row: list[float | None], medians: list[float]) -> list[float]:
        return [median if value is None else float(value) for value, median in zip(row, medians)]

    @staticmethod
    def _quantile(values: list[float], quantile: float) -> float:
        ordered = sorted(values)
        position = (len(ordered) - 1) * quantile
        lower = math.floor(position)
        upper = math.ceil(position)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    @staticmethod
    def _is_finite_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @staticmethod
    def _validate_node(node: Any, depth: int, max_height: int) -> None:
        if not isinstance(node, dict) or depth > max_height:
            raise ValueError("Stored model tree is malformed")
        if "size" in node:
            if set(node) != {"size"} or not isinstance(node["size"], int) or node["size"] < 1:
                raise ValueError("Stored model leaf is malformed")
            return
        if (
            set(node) != {"feature", "split", "left", "right"}
            or not isinstance(node["feature"], int)
            or not 0 <= node["feature"] < len(FEATURE_NAMES)
            or not IsolationForestModel._is_finite_number(node["split"])
        ):
            raise ValueError("Stored model split is malformed")
        IsolationForestModel._validate_node(node["left"], depth + 1, max_height)
        IsolationForestModel._validate_node(node["right"], depth + 1, max_height)
