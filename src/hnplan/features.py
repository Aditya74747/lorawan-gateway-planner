"""Radio-path (line-of-sight) features for a receiver->gateway link.

Every path is sampled every ~5 m. At each sample we compare the straight line between the
antennas (with 4/3-earth curvature) to the terrain and the surface (buildings + trees)
and record how badly the path – and its first Fresnel zone – is obstructed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import ndimage

from .rasters import Grid

C = 299_792_458.0
K_EARTH_R = 4 / 3 * 6_371_000.0

FEATURES = [
    "log_d", "d_m", "fspl_db", "tx_h_agl", "dz_m", "elev_angle_deg",
    "obs_max_m", "obs_frac", "fresnel_frac", "terr_obs_max_m", "terr_obs_frac",
    "bldg_cross", "bldg_len_m", "forest_len_m", "rx_bldg_h", "rx_bdens", "rx_ground_z", "valid_frac",
]
# Computed but NOT used by the model – both failed the leave-one-gateway-out test (CatBoost log-loss):
#   water_len_m  metres over Basis-DLM water: 0.1742 -> 0.1779 (crossing the Neckar stands in for distance)
#   tree_len_m   metres through DOP20 tree canopy: 0.1742 -> 0.1744 (92 % of links cross trees, and
#                DOM1 already counts trees as obstruction in obs_*/fresnel_frac)
# Move them into FEATURES to re-test once more measurements exist.
EXTRA = ["water_len_m", "tree_len_m"]
# Derived from the HNVG documents (TAB Wasser / Ergänzende Bedingungen). Computed for every link and
# kept for analysis, but NOT model inputs: in the ablation (scripts/ablation_placement.py) they did not
# improve validation, and no measured meter sits in a shaft, so a shaft effect cannot be learned.
DOC_FEATURES = ["facing_cos", "rx_setback_m", "shaft_likely"]
# monotonic constraints for the gradient-boosting model (+1 increasing, -1 decreasing)
MONOTONIC = {
    "log_d": -1, "d_m": -1, "fspl_db": -1, "tx_h_agl": +1, "elev_angle_deg": +1,
    "obs_max_m": -1, "obs_frac": -1, "fresnel_frac": -1, "terr_obs_max_m": -1, "terr_obs_frac": -1,
    "bldg_cross": -1, "bldg_len_m": -1, "forest_len_m": -1, "tree_len_m": -1,
    "facing_cos": +1, "shaft_likely": -1,
}


class FeatureBuilder:
    def __init__(self, grid: Grid, radio: dict, gridcfg: dict):
        self.g = grid
        self.f_hz = radio["frequency_mhz"] * 1e6
        self.lam = C / self.f_hz
        self.rx_h = radio["rx_height_m"]
        self.step = gridcfg["profile_step_m"]
        self.kmax = gridcfg["profile_max_samples"]
        self.clear = gridcfg["endpoint_clearance_m"]
        bmask = (grid.bheight > 0).astype(np.float32)
        n = int(round(50 / grid.res)) * 2 + 1
        self.bdens = ndimage.uniform_filter(bmask, size=n)
        self.bnear = ndimage.maximum_filter(grid.bheight, size=5)

    def rx_points(self, x, y, placement=None):
        """placement: optional DataFrame from MeterPlacer (fdx, fdy, setback_m, shaft_likely)."""
        x, y = np.asarray(x, float), np.asarray(y, float)
        ground = self.g.sample(self.g.terrain, x, y)
        nan = np.full(len(x), np.nan)
        pl = placement if placement is not None else {}
        get = lambda k, d: np.asarray(pl[k], float) if k in pl else d
        return dict(x=x, y=y, ground=ground, z=ground + self.rx_h,
                    bh=self.g.sample(self.bnear, x, y, fill=0),
                    bdens=self.g.sample(self.bdens, x, y, fill=0),
                    fdx=get("fdx", nan), fdy=get("fdy", nan),
                    setback=get("setback_m", nan), shaft=get("shaft_likely", np.zeros(len(x))))

    def links(self, rx: dict, ri: np.ndarray, tx: pd.DataFrame, ti: np.ndarray, chunk=2500) -> pd.DataFrame:
        """Features for links (rx[ri[k]] -> tx.iloc[ti[k]])."""
        out = []
        txx, txy, txz, txh = (tx[c].to_numpy(float) for c in ("x", "y", "antenna_z", "h_agl"))
        for s0 in range(0, len(ri), chunk):
            a, b = ri[s0:s0 + chunk], ti[s0:s0 + chunk]
            df = self._chunk(rx["x"][a], rx["y"][a], rx["z"][a], rx["ground"][a], rx["bh"][a],
                             rx["bdens"][a], txx[b], txy[b], txz[b], txh[b])
            d = np.hypot(txx[b] - rx["x"][a], txy[b] - rx["y"][a]).clip(1.0)
            df["facing_cos"] = ((txx[b] - rx["x"][a]) * rx["fdx"][a] + (txy[b] - rx["y"][a]) * rx["fdy"][a]) / d
            df["rx_setback_m"] = rx["setback"][a]
            df["shaft_likely"] = rx["shaft"][a]
            out.append(df[FEATURES + EXTRA + DOC_FEATURES])
        return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=FEATURES + EXTRA + DOC_FEATURES)

    def _chunk(self, rx_x, rx_y, rx_z, rx_g, rx_bh, rx_bd, tx_x, tx_y, tx_z, tx_h):
        g = self.g
        d = np.hypot(tx_x - rx_x, tx_y - rx_y).clip(1.0)
        n = np.clip(np.ceil(d / self.step), 8, self.kmax).astype(int)
        K = int(n.max())
        t = (np.arange(K)[None, :] + 0.5) / n[:, None]
        valid = t < 1.0
        t = np.where(valid, t, 0.999)
        d1 = t * d[:, None]
        d2 = d[:, None] - d1
        px = rx_x[:, None] + t * (tx_x - rx_x)[:, None]
        py = rx_y[:, None] + t * (tx_y - rx_y)[:, None]
        los = rx_z[:, None] + t * (tx_z - rx_z)[:, None] - d1 * d2 / (2 * K_EARTH_R)
        inner = valid & (d1 > self.clear) & (d2 > self.clear)
        ninner = inner.sum(1).clip(1)
        stepm = d / n

        surf = g.sample(g.surface, px.ravel(), py.ravel()).reshape(px.shape)
        terr = g.sample(g.terrain, px.ravel(), py.ravel()).reshape(px.shape)
        bh = (g.sample(g.bheight, px.ravel(), py.ravel(), fill=0).reshape(px.shape) > 0) & inner
        fo = (g.sample(g.forest, px.ravel(), py.ravel(), fill=0).reshape(px.shape) > 0) & inner
        wa = ((g.sample(g.water, px.ravel(), py.ravel(), fill=0).reshape(px.shape) > 0) & inner
              if g.water is not None else np.zeros_like(inner))
        tc = ((g.sample(g.tree, px.ravel(), py.ravel(), fill=0).reshape(px.shape) > 0) & inner
              if g.tree is not None else np.zeros_like(inner))
        h = surf - los
        ht = terr - los
        r1 = np.sqrt(self.lam * d1 * d2 / d[:, None])
        fin = np.isfinite(h) & inner
        with np.errstate(invalid="ignore"):
            obs_max = np.where(fin, h, -np.inf).max(1)
            terr_max = np.where(np.isfinite(ht) & inner, ht, -np.inf).max(1)
            obs_frac = ((h > 0) & fin).sum(1) / ninner
            fres = ((h > -0.6 * r1) & fin).sum(1) / ninner
            terr_frac = ((ht > 0) & inner & np.isfinite(ht)).sum(1) / ninner
        cross = (bh[:, 1:] & ~bh[:, :-1]).sum(1) + bh[:, 0]
        dz = tx_z - rx_z
        df = pd.DataFrame(dict(
            d_m=d, log_d=np.log10(d), fspl_db=20 * np.log10(d / 1000) + 20 * np.log10(self.f_hz / 1e6) + 32.44,
            tx_h_agl=tx_h, dz_m=dz, elev_angle_deg=np.degrees(np.arctan2(dz, d)),
            obs_max_m=np.clip(obs_max, -60, 300), obs_frac=obs_frac, fresnel_frac=fres,
            terr_obs_max_m=np.clip(terr_max, -60, 300), terr_obs_frac=terr_frac,
            bldg_cross=cross.astype(float), bldg_len_m=bh.sum(1) * stepm, forest_len_m=fo.sum(1) * stepm,
            # metres where the direct path runs through tree canopy (below the canopy top)
            tree_len_m=(tc & (h > 0) & fin).sum(1) * stepm,
            water_len_m=wa.sum(1) * stepm,  # open water: no clutter, but reflections – no sign constraint
            rx_bldg_h=rx_bh, rx_bdens=rx_bd, rx_ground_z=rx_g, valid_frac=fin.sum(1) / ninner,
        ))
        # links with no usable geometry at all (outside the surface model) -> leave NaN
        nogeo = ~np.isfinite(dz) | (df["valid_frac"].to_numpy() == 0)
        df.loc[nogeo, ["obs_max_m", "terr_obs_max_m", "obs_frac", "fresnel_frac", "terr_obs_frac"]] = np.nan
        return df
