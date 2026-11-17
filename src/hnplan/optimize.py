"""Gateway placement optimisation on the predicted reception matrix.

Objective (expected number of covered addresses) is monotone submodular, so the greedy
algorithm is within (1 - 1/e) ≈ 63 % of the optimum – and in practice usually optimal for
small k. The same algorithm runs in the browser (web/app.js) for interactive use.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from .config import Config

SWAP_MAX_CANDIDATES = 100  # keep in sync with web/app.js (SWAP_MAX)


class Planner:
    def __init__(self, cfg: Config, device: str = "mix", scenario: str = "today"):
        """device: "mix" = today's meter mix (default), "weak" = the weakest meter type (conservative).
        scenario: "today" or "future" (planned buildings from the land-use plans as obstacles + their new
        meters as extra demand; see future.py)."""
        if scenario == "future":
            from .pipeline import future_cell_table
            self.cells = future_cell_table(cfg)
            pr = pd.read_parquet(cfg.processed("predictions_future.parquet"))
        else:
            self.cells = pd.read_parquet(cfg.processed("cells.parquet"))
            pr = pd.read_parquet(cfg.processed("predictions.parquet"))
        self.scenario = scenario
        self.sites = pd.read_parquet(cfg.processed("sites.parquet"))
        col = "p_weak" if device == "weak" and "p_weak" in pr else "p"
        self.P = np.zeros((len(self.cells), len(self.sites)), np.float32)
        self.P[pr.cell_idx.to_numpy(), pr.site_idx.to_numpy()] = pr[col].to_numpy()
        self.w = self.cells["n_addr"].to_numpy(float)
        self.W = self.w.sum()
        self.existing = list(np.flatnonzero(self.sites.kind.to_numpy() == "existing"))
        self.candidates = list(np.flatnonzero(self.sites.kind.to_numpy() == "candidate"))
        # challenge provider rule: a device is served by a gateway within `device_radius_m`
        self.radius = float(cfg.raw["radio"].get("device_radius_m", 300))
        d = np.hypot(self.cells.x.to_numpy()[:, None] - self.sites.x.to_numpy()[None, :],
                     self.cells.y.to_numpy()[:, None] - self.sites.y.to_numpy()[None, :])
        self.NEAR = (d <= self.radius).astype(np.float32)

    def site_index(self, ids) -> list[int]:
        m = {s: i for i, s in enumerate(self.sites.site_id)}
        return [m[i] for i in ids]

    # --- probabilities for a set of active sites
    def miss(self, active) -> np.ndarray:
        return np.prod(1 - self.P[:, list(active)], axis=1) if len(active) else np.ones(len(self.w))

    def p_ge2(self, active) -> np.ndarray:
        if len(active) < 2:
            return np.zeros(len(self.w))
        Q = 1 - self.P[:, list(active)]
        q0 = np.prod(Q, axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            q1 = np.sum(self.P[:, list(active)] * np.where(Q > 0, q0[:, None] / Q, 0), axis=1)
        return np.clip(1 - q0 - q1, 0, 1)

    def within_radius(self, active) -> float:
        """Share of addresses within `device_radius_m` of at least one active gateway (provider rule)."""
        if not len(active):
            return 0.0
        return float((self.w * (self.NEAR[:, list(active)].max(axis=1) > 0)).sum() / self.W)

    def summary(self, active) -> dict:
        cov = 1 - self.miss(active)
        red = self.p_ge2(active)
        return dict(coverage=float((self.w * cov).sum() / self.W), redundancy=float((self.w * red).sum() / self.W),
                    gap_addresses=float((self.w * (cov < 0.5)).sum()), within_radius=self.within_radius(active))

    def _score(self, active, objective):
        s = self.summary(active)
        if objective == "radius":
            return s["within_radius"]
        return s["coverage"] if objective == "coverage" else 0.5 * s["coverage"] + 0.5 * s["redundancy"]

    def greedy(self, k: int, objective: str = "coverage", disabled=(), pool=None) -> dict:
        active = [i for i in self.existing if i not in set(disabled)]
        pool = list(pool if pool is not None else self.candidates)
        base = self.summary(active)
        steps = []
        for _ in range(k):
            cur = self._score(active, objective)
            best, gain = None, -1.0
            for c in pool:
                if c in active:
                    continue
                g = self._score(active + [c], objective) - cur
                if g > gain:
                    best, gain = c, g
            if best is None:
                break
            active.append(best)
            s = self.summary(active)
            steps.append(dict(site_id=self.sites.site_id[best], name=self.sites.name[best], gain=gain, **s))
        return dict(objective=objective, baseline=base, steps=steps)

    def failure_analysis(self) -> list[dict]:
        base = self.summary(self.existing)
        out = []
        for i in self.existing:
            s = self.summary([j for j in self.existing if j != i])
            out.append(dict(site_id=self.sites.site_id[i], name=self.sites.name[i],
                            coverage_loss=base["coverage"] - s["coverage"],
                            redundancy_loss=base["redundancy"] - s["redundancy"]))
        return sorted(out, key=lambda d: -d["coverage_loss"])

    # --- "how few of the approved sites are enough?" ---------------------------------------------
    def _cov(self, M: np.ndarray, cols) -> float:
        """Expected share of addresses heard by >= 1 of the sites `cols` (M = P, or NEAR for the 300 m rule)."""
        q0 = np.prod(1 - M[:, list(cols)], axis=1) if len(cols) else np.ones(len(self.w))
        return float((self.w * (1 - q0)).sum() / self.W)

    def _greedy_order(self, M: np.ndarray, base, pool) -> tuple[list[int], float, list[float]]:
        """Order `pool` greedily by expected newly covered addresses on top of `base` (vectorised).
        Returns (order, coverage of base, coverage after each added site)."""
        pool = list(pool)
        q0 = np.prod(1 - M[:, list(base)], axis=1) if len(base) else np.ones(len(self.w))
        Mp = M[:, pool]
        left = np.ones(len(pool), bool)
        order, curve = [], []
        cov0 = float(1 - (self.w * q0).sum() / self.W)
        while left.any():
            gains = (self.w * q0) @ Mp          # expected newly covered addresses per remaining site
            gains[~left] = -np.inf
            j = int(np.argmax(gains))
            left[j] = False
            order.append(pool[j])
            q0 = q0 * (1 - Mp[:, j])
            curve.append(float(1 - (self.w * q0).sum() / self.W))
        return order, cov0, curve

    def _prune(self, M, base, chosen, target) -> list[int]:
        """Repeatedly drop the site whose removal costs least, while coverage stays >= target."""
        chosen = list(chosen)
        while len(chosen) > 1:
            rest = {i: self._cov(M, base + [c for c in chosen if c != i]) for i in chosen}
            drop = max(rest, key=rest.get)
            if rest[drop] < target:
                break
            chosen.remove(drop)
        return chosen

    def _swap(self, M, base, chosen, target) -> list[int] | None:
        """Replace two chosen sites by one unused site if the target still holds (saves one gateway).
        For each pair all unused sites are evaluated at once. Returns the smaller set, or None."""
        out = [c for c in self.candidates if c not in chosen]
        if not out or len(chosen) < 2:
            return None
        keep_out = 1 - M[:, out]
        for a, b in itertools.combinations(chosen[::-1], 2):  # weakest (latest) sites first
            rest = [c for c in chosen if c not in (a, b)]
            q0 = np.prod(1 - M[:, base + rest], axis=1)
            cov = 1 - ((self.w * q0) @ keep_out) / self.W     # coverage with each unused site added
            j = int(np.argmax(cov))
            if cov[j] >= target:
                return rest + [out[j]]
        return None

    def min_sites(self, target: float, objective: str = "coverage", disabled=()) -> dict:
        """Fewest candidate sites that lift coverage to >= `target` (on top of the existing gateways).

        1. greedy order – for this monotone submodular objective provably within (1 - 1/e) of the best
           possible set of the same size
        2. take the shortest greedy prefix that reaches the target
        3. prune: drop sites that the others make redundant
        4. swap: replace two sites by one better-placed site while the target holds; repeat 3–4
           -> the result is minimal: no site can be removed and no 2-for-1 exchange saves a site
        5. re-order the final set greedily -> a build order (most valuable site first)
        """
        M = self.NEAR if objective == "radius" else self.P
        base = [i for i in self.existing if i not in set(disabled)]
        order, cov0, curve = self._greedy_order(M, base, self.candidates)
        maximum = curve[-1] if curve else cov0
        k = next((i + 1 for i, c in enumerate(curve) if c >= target), None)
        chosen = list(order[:k]) if k is not None else list(order)
        swaps = 0
        if k is not None:
            chosen = self._prune(M, base, chosen, target)
            # 2-for-1 swaps cost O(k² · candidates · cells): skipped for large generated candidate sets (other cities)
            while len(self.candidates) <= SWAP_MAX_CANDIDATES and (better := self._swap(M, base, chosen, target)) is not None:
                chosen = self._prune(M, base, better, target)
                swaps += 1
        chosen, _, steps = self._greedy_order(M, base, chosen)
        return dict(objective=objective, target=target, reached=k is not None, k=len(chosen), greedy_k=k, swaps=swaps,
                    baseline=cov0, coverage=steps[-1] if steps else cov0, maximum=maximum, curve=curve,
                    sites=[self.sites.site_id[i] for i in chosen], steps=steps)
