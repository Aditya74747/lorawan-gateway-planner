"""Weather and reception: does rain, humidity or temperature change what the gateways hear?

Coverage is a property of the place, weather changes hour by hour, so weather is analysed per hour
against each link's own normal behaviour:
  1. signal  – RSSI and SNR of every received message minus the median of its meter–gateway link
  2. loss    – messages lost, from gaps in each meter's frame counter, placed in the hour they were due
               (looking only at received messages would hide exactly the losses rain might cause)
  3. context – the measured week compared with the whole weather year (how representative is it?)
Uncertainty: 95 % intervals from a bootstrap over whole hours (messages of one hour are not independent).
If an effect were found, the planner would need a weather margin on its coverage target.

Sources
  * Open-Meteo historical API (no key): `hnplan weather --fetch`
  * or any CSV you download (columns: time + precipitation / relative_humidity_2m /
    temperature_2m) placed in data/raw – the data selector picks it up automatically.
"""
from __future__ import annotations

import io

import numpy as np
import pandas as pd
import requests

from .config import Config

URL = "https://archive-api.open-meteo.com/v1/archive"
VARS = ["precipitation", "relative_humidity_2m", "temperature_2m", "rain", "snowfall", "cloud_cover", "wind_speed_10m"]
WET_MM = 0.1   # an hour counts as rainy above this precipitation


def fetch_open_meteo(lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    r = requests.get(URL, params=dict(latitude=lat, longitude=lon, start_date=start, end_date=end,
                                      hourly=",".join(VARS), timezone="UTC"), timeout=60)
    r.raise_for_status()
    h = r.json()["hourly"]
    df = pd.DataFrame(h)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def read_weather_csv(raw: bytes) -> pd.DataFrame:
    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.lower().startswith("time"))  # skip open-meteo header block
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
    df.columns = [c.split(" (")[0].strip() for c in df.columns]
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def _boot(hours: pd.DataFrame, stat, n: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    """Statistic on per-hour rows + 95 % interval from resampling whole hours."""
    rng = np.random.default_rng(seed)
    est = stat(hours)
    idx = rng.integers(0, len(hours), (n, len(hours)))
    vals = np.array([stat(hours.iloc[i]) for i in idx])
    vals = vals[np.isfinite(vals)]
    lo, hi = (np.percentile(vals, [2.5, 97.5]) if len(vals) else (np.nan, np.nan))
    return float(est), float(lo), float(hi)


def _lost_frames(tx: pd.DataFrame) -> pd.DataFrame:
    """Messages that were sent but never received: gaps in each meter's frame counter, spread evenly
    over the time between the received neighbours (counter resets / huge gaps are skipped)."""
    msgs = tx.drop_duplicates("id").dropna(subset=["fcnt"]).sort_values(["device_id", "ts"])
    rows = []
    for _, g in msgs.groupby("device_id"):
        f, t = g.fcnt.to_numpy(), g.ts.to_numpy()
        for i in range(1, len(g)):
            d = int(f[i] - f[i - 1])
            if 2 <= d <= 50:
                step = (t[i] - t[i - 1]) / d
                rows.extend(t[i - 1] + step * k for k in range(1, d))
    lost = pd.DataFrame({"ts": pd.to_datetime(rows, utc=True)}) if rows else pd.DataFrame({"ts": pd.Series([], dtype="datetime64[ns, UTC]")})
    return lost


def analyse(cfg: Config, weather: pd.DataFrame) -> dict:
    tx = pd.read_parquet(cfg.processed("transmissions.parquet"))
    weather = weather.copy()
    weather["time"] = pd.to_datetime(weather["time"], utc=True)
    W = weather.set_index("time")
    tx["hour"] = tx.ts.dt.floor("h")
    link = tx.groupby(["device_id", "gateway_id"])
    tx["rssi_res"] = tx.rssi - link.rssi.transform("median")
    tx["snr_res"] = tx.snr - link.snr.transform("median")

    # ---- per hour: signal residuals, messages received and lost
    msgs = tx.drop_duplicates("id")
    lost = _lost_frames(tx)
    per_h = tx.groupby("hour").agg(rssi_res=("rssi_res", "mean"), snr_res=("snr_res", "mean"), receptions=("id", "size"))
    per_h["received"] = msgs.groupby("hour").size()
    per_h["lost"] = lost.groupby(lost.ts.dt.floor("h")).size() if len(lost) else 0
    per_h = per_h.fillna({"received": 0, "lost": 0}).join(W, how="inner")
    if per_h.empty:
        return dict(error="no overlap between weather data and radio data – check the date range of the weather file")
    per_h["wet"] = per_h["precipitation"] > WET_MM

    def diff(col, w="receptions"):          # weighted mean in wet hours minus dry hours
        def f(h):
            a, b = h[h.wet], h[~h.wet]
            if a[w].sum() == 0 or b[w].sum() == 0:
                return np.nan
            return np.average(a[col], weights=a[w]) - np.average(b[col], weights=b[w])
        return f

    def deliv(wet):                          # delivery ratio = received / (received + lost)
        def f(h):
            s = h[h.wet == wet]
            n = s.received.sum() + s.lost.sum()
            return s.received.sum() / n if n else np.nan
        return f

    def slope(var, col, per):                # weighted least-squares slope, per `per` units of var
        def f(h):
            x, y, w = h[var].to_numpy(float), h[col].to_numpy(float), h["receptions"].to_numpy(float)
            if np.ptp(x) == 0:
                return np.nan
            xm, ym = np.average(x, weights=w), np.average(y, weights=w)
            return per * np.sum(w * (x - xm) * (y - ym)) / np.sum(w * (x - xm) ** 2)
        return f

    ci = lambda t: dict(value=t[0], lo=t[1], hi=t[2])  # noqa: E731
    out = dict(
        week=dict(start=str(per_h.index.min()), end=str(per_h.index.max()), hours=int(len(per_h)),
                  receptions=int(per_h.receptions.sum()), messages=int(per_h.received.sum()), lost=int(per_h.lost.sum()),
                  rain_hours=int(per_h.wet.sum()), rain_mm=float(per_h.precipitation.sum()),
                  temp_min=float(per_h.temperature_2m.min()), temp_max=float(per_h.temperature_2m.max()),
                  rh_min=float(per_h.relative_humidity_2m.min()), rh_max=float(per_h.relative_humidity_2m.max())),
        rain=dict(rssi_db=ci(_boot(per_h, diff("rssi_res"))), snr_db=ci(_boot(per_h, diff("snr_res"))),
                  delivery_wet=ci(_boot(per_h, deliv(True))), delivery_dry=ci(_boot(per_h, deliv(False)))),
        trend=dict(rssi_db_per_10C=ci(_boot(per_h, slope("temperature_2m", "rssi_res", 10))),
                   rssi_db_per_10pct_rh=ci(_boot(per_h, slope("relative_humidity_2m", "rssi_res", 10))),
                   snr_db_per_10pct_rh=ci(_boot(per_h, slope("relative_humidity_2m", "snr_res", 10)))),
    )

    # ---- how representative is the week for the whole weather year?
    yr = W.loc[W.index.max() - pd.Timedelta(days=365):] if len(W) > 24 * 60 else None
    if yr is not None:
        wk_temp = per_h.temperature_2m.mean()
        weekly = yr.precipitation.gt(WET_MM).resample("7D").sum()
        out["year"] = dict(
            start=str(yr.index.min()), end=str(yr.index.max()), hours=int(len(yr)),
            rain_hours_share=float(yr.precipitation.gt(WET_MM).mean()),
            rain_hours_per_week_median=float(weekly.median()), rain_hours_per_week_p90=float(weekly.quantile(0.9)),
            temp_min=float(yr.temperature_2m.min()), temp_max=float(yr.temperature_2m.max()),
            temp_p5=float(yr.temperature_2m.quantile(0.05)), temp_p95=float(yr.temperature_2m.quantile(0.95)),
            week_temp_percentile=float((yr.temperature_2m < wk_temp).mean()),
            week_rain_hours_percentile=float((weekly < per_h.wet.sum()).mean()),
            share_of_year_temp_range_covered=float(
                (per_h.temperature_2m.max() - per_h.temperature_2m.min()) /
                max(yr.temperature_2m.quantile(0.95) - yr.temperature_2m.quantile(0.05), 1e-9)),
            frost_hours=int((yr.temperature_2m < 0).sum()),
            snow_hours=int((yr.get("snowfall", pd.Series(0, index=yr.index)) > 0).sum()),
            max_rain_mm_h=float(yr.precipitation.max()), week_max_rain_mm_h=float(per_h.precipitation.max()))

    # ---- verdict in words
    r, s = out["rain"]["rssi_db"], out["rain"]["delivery_wet"]
    found = (r["lo"] > 0 or r["hi"] < 0)
    out["verdict"] = (
        f"In the measured week rain changed the signal by {r['value']:+.2f} dB (95 % interval {r['lo']:+.2f} … {r['hi']:+.2f} dB) – "
        + ("a measurable effect." if found else "no measurable effect; any real effect is smaller than about "
           f"{max(abs(r['lo']), abs(r['hi'])):.1f} dB.")
        + f" Delivery in rainy hours {100 * s['value']:.0f} % vs {100 * out['rain']['delivery_dry']['value']:.0f} % dry."
        + (f" But the week covered only {100 * out['year']['share_of_year_temp_range_covered']:.0f} % of the year's temperature range"
           " – summer heat, frost, snow and foliage are untested; a full year of radio data is needed to confirm."
           if "year" in out else ""))
    weather.to_parquet(cfg.processed("weather.parquet"))
    return out
