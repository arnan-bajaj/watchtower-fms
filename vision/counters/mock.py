"""Mock counter: no model, no camera needed. Emits random fuel at `rate_per_s`
(Poisson). Use it to rehearse the whole event flow (refs, emcee, TBA) before the
model is ready. Set rate_per_s ~3-5 to look like a real match."""
from __future__ import annotations

import random

from .base import Counter


class MockCounter(Counter):
    NAME = "mock"
    DESCRIPTION = "Random fuel for rehearsals; no model or camera (source: none)"
    OPTIONS = {"rate_per_s": "average fuel per second (3.0)"}

    def __init__(self, cfg):
        super().__init__(cfg)
        self.rate = float(cfg.get("rate_per_s", 3.0))
        self.last_t = None

    def process(self, frame, t):
        dt = 0.0 if self.last_t is None else max(0.0, t - self.last_t)
        self.last_t = t
        lam, n, p = self.rate * dt, 0, random.random()
        # Poisson sample via inversion (lam is small per frame)
        import math
        term = math.exp(-lam)
        acc = term
        while p > acc and n < 50:
            n += 1
            term *= lam / n
            acc += term
        self.total += n
        return n
