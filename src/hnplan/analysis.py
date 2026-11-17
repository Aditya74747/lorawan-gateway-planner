"""'Understand' insights from the measured data (no model involved)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config
from .pipeline import device_stats


def insights(cfg: Config) -> dict:
    P = cfg.processed
    tx = pd.read_parquet(P("transmissions.parquet"))
    meters = pd.read_parquet(P("meters.parquet"))
    L = pd.read_parquet(P("train_links.parquet"))
    sites = pd.read_parquet(P("sites.parquet"))
    dev, link = device_stats(tx)
    heard = set(tx.device_id)
    meters["heard"] = meters.device_id.isin(heard)

    status = (meters.groupby("meter_state").heard.agg(["size", "mean"])
              .rename(columns={"size": "meters", "mean": "heard_share"}).reset_index().to_dict("records"))

    H = L[L.heard == 1]
    bins = [0, 250, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000]
    H = H.assign(dbin=pd.cut(H.d_m, bins))
    rssi_dist = [dict(d_from=int(i.left), d_to=int(i.right), n=int(len(g)), rssi_med=float(g.rssi_med.median()),
                      snr_med=float(g.snr_med.median()))
                 for i, g in H.groupby("dbin", observed=True) if len(g) >= 3]
    # free-space expectation with the link budget from config.yaml (meter 14 dBm, gateway antenna gain)
    r = cfg.raw["radio"]
    fspl = lambda d: 20 * np.log10(d / 1000) + 20 * np.log10(r["frequency_mhz"]) + 32.44  # noqa: E731
    near = H[H.d_m < 500]
    eirp = r.get("device_tx_dbm", 14) + r.get("gateway_antenna_dbi", 2)
    excess = float((eirp - fspl(near.d_m) - near.rssi_med).median()) if len(near) else None

    msgs = tx.drop_duplicates("id")
    sf = msgs.groupby("sf").agg(n=("id", "size"), toa_ms=("toa_ms", "median")).reset_index()
    sf["share"] = sf.n / sf.n.sum()
    sf["airtime_share"] = sf.n * sf.toa_ms / (sf.n * sf.toa_ms).sum()

    gw_names = dict(zip(sites.site_id, sites.name))
    load = tx.groupby("gateway_id").agg(receptions=("id", "size"), devices=("device_id", "nunique")).reset_index()
    load["name"] = load.gateway_id.map(gw_names).fillna("unknown location")
    load["share"] = load.receptions / load.receptions.sum()

    n_gw = link.groupby("device_id").gateway_id.nunique()
    pdr = dev.pdr.dropna()
    scatter = H.sample(min(len(H), 600), random_state=1)[["d_m", "rssi_med", "site_name"]]
    return dict(
        meters_total=int(len(meters)), meters_heard=int(meters.heard.sum()),
        meter_status=status,
        broken_coordinates_repaired=int((meters.coord_source.fillna("").str.startswith("geocoded")).sum()),
        rssi_by_distance=rssi_dist, rssi_scatter=scatter.round(1).to_dict("records"),
        excess_loss_db_under_500m=excess,
        sf_mix=sf.round(4).to_dict("records"),
        gateway_load=load.sort_values("receptions", ascending=False).round(4).to_dict("records"),
        redundancy=dict(one=float((n_gw == 1).mean()), two_plus=float((n_gw >= 2).mean())),
        delivery_ratio=dict(median=float(pdr.median()), share_below_80=float((pdr < 0.8).mean()),
                            hist=np.histogram(pdr, bins=10, range=(0, 1))[0].tolist()),
        uplink_interval_h=float(msgs.sort_values("ts").groupby("device_id").ts.diff().dt.total_seconds()
                                .median() / 3600),
        week=[str(msgs.ts.min()), str(msgs.ts.max())],
        # what the radio data says about the hardware (compare with the datasheet values in config.yaml)
        radio_observed=dict(
            rssi_floor_dbm=float(tx.rssi.quantile(0.01)),
            snr_min_by_sf={int(k): float(v) for k, v in tx.groupby("sf").snr.min().items()},
            adr_share=float((msgs["meta.adr"].astype(str).str.lower() == "true").mean()) if "meta.adr" in msgs else None,
            msgs_per_meter_day=float(msgs.groupby("device_id").size().median() /
                                     max((msgs.ts.max() - msgs.ts.min()).total_seconds() / 86400, 1)),
            payload_bytes=float(pd.to_numeric(msgs.get("meta.size"), errors="coerce").median()) if "meta.size" in msgs else None,
        ),
        hardware=cfg.raw.get("hardware", {}),
        link_budget=dict(device_tx_dbm=r.get("device_tx_dbm"), gateway_antenna_dbi=r.get("gateway_antenna_dbi"),
                         gateway_sensitivity_dbm=r.get("gateway_sensitivity_dbm")),
    )
