"""Hub count calibration (/control -> Setup: counts per ball). No cv2, so tests can import it."""


class Scaler:
    """Calibration: a counter that sees one ball as `scale` counts reports raw/scale balls. Whole
    balls only; the remainder carries over. A new scale applies from the next count, no jump."""

    def __init__(self, scale=1.0):
        self.raw = self.out = 0
        self.set(scale)

    def set(self, scale):
        self.scale = max(0.05, float(scale or 1))
        self.raw0, self.out0 = self.raw, self.out

    def add(self, n):
        self.raw += n
        target = self.out0 + int((self.raw - self.raw0) / self.scale + 1e-9)
        d, self.out = target - self.out, max(self.out, target)
        return max(0, d)
