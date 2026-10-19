"""Deterministic, label-keyed random streams.

Every stochastic quantity (start jitter, task-duration jitter, readings, fault times) is
drawn from a stream keyed by ``(seed, labels...)``. Streams are independent of execution
order, so two variants run with the same seed see *identical* random draws (paired design).
"""
from __future__ import annotations

import hashlib

import numpy as np


def stable_seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def stable_rng(*parts: object) -> np.random.Generator:
    return np.random.default_rng(stable_seed(*parts))
