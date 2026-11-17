"""Readers + cleaning for the challenge CSVs (HNVG)."""
from __future__ import annotations

import io
import re

import geopandas as gpd
import numpy as np
import pandas as pd

from .discover import Source

COORD_RE = re.compile(r"^\s*-?\d+(?:[.,]\d+)?\s*$")


def read_csv(src: Source, **kw) -> pd.DataFrame:
    raw = src.open().read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sep = ";" if text.split("\n", 1)[0].count(";") >= text.split("\n", 1)[0].count(",") else ","
    df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, **kw)
    df.columns = [c.strip() for c in df.columns]
    return df


def parse_coord(s: pd.Series) -> pd.Series:
    """German decimal comma -> float. Malformed values (e.g. '49,1169412,632477') -> NaN."""
    s = s.astype(str)
    ok = s.str.match(COORD_RE)
    out = pd.to_numeric(s.where(ok).str.replace(",", ".", regex=False), errors="coerce")
    return out


def to_points(df: pd.DataFrame, lon="longitude", lat="latitude", crs="EPSG:25832") -> gpd.GeoDataFrame:
    g = gpd.GeoDataFrame(df.copy(), geometry=gpd.points_from_xy(df[lon], df[lat]), crs="EPSG:4326")
    return g.to_crs(crs)


def load_transmissions(src: Source) -> pd.DataFrame:
    t = read_csv(src)
    t["ts"] = pd.to_datetime(t["transceived_at"], utc=True, format="mixed")
    for c in ["gateways.rssi", "gateways.snr", "meta.frame_count_up", "meta.lorawan_toa_ms",
              "meta.region_meta.spreadingfactor", "meta.frequency", "gateway_anzahl"]:
        if c in t:
            t[c] = pd.to_numeric(t[c], errors="coerce")
    return t.rename(columns={"gateways.gateway_id": "gateway_id", "gateways.rssi": "rssi",
                             "gateways.snr": "snr", "meta.frame_count_up": "fcnt",
                             "meta.region_meta.spreadingfactor": "sf", "meta.lorawan_toa_ms": "toa_ms"})


def load_meters(meta_src: Source, map_src: Source) -> pd.DataFrame:
    m = read_csv(meta_src)
    mp = read_csv(map_src)
    m["lat"] = parse_coord(m["latitude"])
    m["lon"] = parse_coord(m["longitude"])
    # sanity window for Heilbronn – anything outside is treated as broken
    bad = ~(m.lat.between(48.9, 49.4) & m.lon.between(8.9, 9.5))
    m.loc[bad, ["lat", "lon"]] = np.nan
    m = m.merge(mp[["device_id", "zaehlernummer_id"]], on="zaehlernummer_id", how="left")
    m["address"] = (m["street"].fillna("") + " " + m["house_number"].fillna("")).str.strip()
    return m


def load_gateways(src: Source) -> pd.DataFrame:
    g = read_csv(src).rename(columns={"gateways.gateway_id": "site_id", "Anzeigename": "name"})
    g["lat"], g["lon"] = parse_coord(g["latitude"]), parse_coord(g["longitude"])
    g["kind"] = "existing"
    return g[["site_id", "name", "lat", "lon", "kind"]]


def load_candidates(src: Source) -> pd.DataFrame:
    c = read_csv(src).rename(columns={"potential_gateway_location": "site_id"})
    c["lat"], c["lon"] = parse_coord(c["latitude"]), parse_coord(c["longitude"])
    c["name"] = "Candidate " + c["site_id"]
    c["kind"] = "candidate"
    return c[["site_id", "name", "lat", "lon", "kind"]]


def load_house_coords(src: Source, bbox: tuple[float, float, float, float]) -> pd.DataFrame:
    """LGL Hauskoordinaten (adressen-bw.txt, state-wide, UTM 32) -> address points inside bbox.
    A numeric Adressierungszusatz is a sub-number ("34" + "1" -> "34/1"), letters are appended ("12a")."""
    l, b, r, t = bbox
    parts = []
    with src.open() as fh:
        for ch in pd.read_csv(fh, sep=";", dtype=str, encoding="utf-8", chunksize=500_000,
                              usecols=["gmd", "str", "hnr", "adz", "ostwert", "nordwert"]):
            x, y = pd.to_numeric(ch.ostwert, errors="coerce"), pd.to_numeric(ch.nordwert, errors="coerce")
            m = x.between(l, r) & y.between(b, t)
            parts.append(ch[m].assign(x=x[m], y=y[m]))
    hk = pd.concat(parts, ignore_index=True)
    adz = hk["adz"].fillna("").str.strip()
    suffix = np.where(adz.str.fullmatch(r"\d+"), "/" + adz, adz)
    hk["address"] = hk["str"].str.strip() + " " + hk["hnr"].str.strip() + suffix
    return hk[["gmd", "address", "x", "y"]]


def load_addresses(src: Source) -> pd.DataFrame:
    a = read_csv(src)
    a.columns = ["plz", "city", "street"][: len(a.columns)] + list(a.columns[3:])
    return a
