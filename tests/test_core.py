import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from hnplan.config import load_config
from hnplan.discover import discover
from hnplan.geocode import norm_addr
from hnplan.io import parse_coord


def _cfg(tmp_path):
    base = yaml.safe_load(open(load_config().root / "config.yaml"))
    base["paths"] = {"raw": str(tmp_path / "raw"), "processed": str(tmp_path / "proc"), "web_data": str(tmp_path / "web")}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(base))
    return load_config(p)


def test_selector_picks_only_needed_files(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for n in ["transmission_data.csv", "transmitter meta data.csv", "mapping_transmitter_id.csv",
              "gateway_meta_data.csv", "potential_gw_location.csv", "HN_Adresses.csv"]:
        (raw / f"84cd939d-{n}").write_text("a;b\n1;2\n")
    (raw / "IMG_1897.jpeg").write_bytes(b"x")
    with zipfile.ZipFile(raw / "ALKIS-oE_080910_Heilbronn_shp.zip", "w") as z:
        for n in ["gebaeudeBauwerke.shp", "flurstueck.shp", "nutzung.shp", "nutzungFlurstueck.shp", "GOVDATA.pdf"]:
            z.writestr(n, "x")
    with zipfile.ZipFile(raw / "LoD1_32_511_5440_2_bw.zip", "w") as z:
        z.writestr("d/LoD1_32_511_5440_1_BW.gml", "x")
    with zipfile.ZipFile(raw / "LoD2_32_511_5440_2_bw.zip", "w") as z:
        z.writestr("d/LoD2_32_511_5440_1_BW.gml", "x")
    with zipfile.ZipFile(raw / "dom1_32_511_5440_2_bw.zip", "w") as z:
        z.writestr("d/dom1_32_511_5440_1_bw_2017.tif", "x")
        z.writestr("d/dom1_32_511_5440_1_bw_2017.tfw", "x")
    sel = discover(_cfg(tmp_path), verbose=False)
    assert len(sel["alkis_buildings"]) == 1 and len(sel["alkis_landuse"]) == 1  # not nutzungFlurstueck
    assert [s.name for s in sel["lod_gml"]] == ["LoD2_32_511_5440_1_BW.gml"]     # LoD2 preferred
    assert [s.name for s in sel["dom1"]] == ["dom1_32_511_5440_1_bw_2017.tif"]    # .tfw ignored
    assert sel["dgm1"] == [] and sel["weather"] == []


def test_broken_coordinates_become_nan():
    s = parse_coord(pd.Series(["49,1169412,632477", "49,138894", "9.191", "x"]))
    assert np.isnan(s[0]) and s[1] == 49.138894 and s[2] == 9.191 and np.isnan(s[3])


def test_address_normalisation():
    assert norm_addr("Ludwigsburger Str. 16") == norm_addr("Ludwigsburger Straße 16")
    assert norm_addr("Gymnasiumstraße 31/1") == norm_addr("Gymnasiumstr. 31/1")


def test_greedy_matches_bruteforce_on_toy_problem():
    from hnplan.optimize import Planner
    pl = Planner.__new__(Planner)
    rng = np.random.default_rng(1)
    pl.P = rng.uniform(0, 0.8, (60, 6)).astype(np.float32) * (rng.uniform(size=(60, 6)) > 0.5)
    pl.w = rng.integers(1, 20, 60).astype(float)
    pl.W = pl.w.sum()
    pl.existing, pl.candidates = [0], [1, 2, 3, 4, 5]
    pl.NEAR = (pl.P > 0.5).astype(np.float32)
    pl.sites = pd.DataFrame(dict(site_id=list("abcdef"), name=list("abcdef")))
    g = pl.greedy(1)["steps"][0]
    best = max(pl.candidates, key=lambda c: pl.summary([0, c])["coverage"])
    assert g["site_id"] == pl.sites.site_id[best]


def test_dgm_xyz_is_pooled_to_grid(tmp_path):
    from hnplan.discover import Source
    from hnplan.rasters import _pool_tiles
    xs, ys = np.meshgrid(np.arange(1000.5, 1010), np.arange(2009.5, 2000, -1))
    z = 100 + (xs - 1000) // 5  # 100 in the left 5 m column, 101 in the right one
    p = tmp_path / "dgm1_32_001_0002_1_bw_2024.xyz"
    np.savetxt(p, np.c_[xs.ravel(), ys.ravel(), z.ravel()], fmt="%.2f")
    out = _pool_tiles([Source("dgm1", "", str(p), p.stat().st_size)], (2, 2), 1000.0, 2010.0, 5.0, "mean")
    assert np.allclose(out, [[100, 101], [100, 101]])


def test_house_coordinate_suffixes(tmp_path):
    from hnplan.discover import Source
    from hnplan.io import load_house_coords
    p = tmp_path / "adressen-bw.txt"
    p.write_text("gmd;str;hnr;adz;ostwert;nordwert\nHeilbronn;Gymnasiumstraße;34;1;514000.0;5442000.0\n"
                 "Heilbronn;Bachstraße;12;a;514001.0;5442001.0\nHeilbronn;Weg;3;;514002.0;5442002.0\n"
                 "Stuttgart;Fern;1;;400000.0;5400000.0\n", encoding="utf-8")
    hk = load_house_coords(Source("house_coords", "", str(p), 1), (510000, 5440000, 520000, 5450000))
    assert hk.address.tolist() == ["Gymnasiumstraße 34/1", "Bachstraße 12a", "Weg 3"]
    assert norm_addr(hk.address[0]) == norm_addr("Gymnasiumstr. 34/1")


def test_dop_rgbi_green_share(tmp_path):
    import rasterio
    from rasterio.transform import from_origin

    from hnplan.discover import Source
    from hnplan.rasters import _pool_green
    a = np.zeros((4, 50, 50), np.uint8)  # 10 m x 10 m at 0.2 m
    a[0], a[3] = 60, 30                  # bare: NIR < red
    a[0, :, :25], a[3, :, :25] = 30, 120  # left half vegetation: NDVI 0.6
    p = tmp_path / "dop20rgbi_32_000_0000_1_bw_2024.tif"
    with rasterio.open(p, "w", driver="GTiff", width=50, height=50, count=4, dtype="uint8",
                       transform=from_origin(1000, 2010, 0.2, 0.2)) as w:
        w.write(a)
    out = _pool_green([Source("dop20", "", str(p), 1)], (2, 2), 1000.0, 2010.0, 5.0)
    assert np.allclose(out, [[1, 0], [1, 0]])


def test_selector_picks_only_the_four_water_documents(tmp_path):
    raw = tmp_path / "raw" / "hnvg_docs"
    raw.mkdir(parents=True)
    keep = ["45f95e70-tab-wasser.pdf", "2385faaf-Ergaenzende-Bedingungen-AVBWasserV.pdf",
            "f0d0f5ad-Montagepreise-Wasserversorgung-Systemleistungen_HNVG.pdf", "430c256b-Preisblatt-AVB_WasserV.pdf"]
    decoys = ["tab-gas.pdf", "TAB_Heizwassernetz-Innenstadt.pdf", "Ergaenzende-Bedingungen-GasGVV.pdf",
              "Ergaenzende-Bedingungen-NDAV-HNVG.pdf", "Preisblatt05_HNVG-2025.pdf", "GasGVV.pdf",
              "AVBWasserV_-_nichtamtliches_Inhaltsverzeichnis.mhtml", "netzentgeltrechner-2026.xlsx"]
    for n in keep + decoys:
        (raw / n).write_bytes(b"%PDF-1.4")
    cfg = _cfg(tmp_path)
    for k in list(cfg.raw["datasets"]):
        if cfg.raw["datasets"][k].get("required"):
            cfg.raw["datasets"][k]["required"] = False
    sel = discover(cfg, verbose=False)
    got = {k: [s.name for s in sel[k]] for k in ["tab_wasser", "water_conditions", "water_service_prices", "water_tariff"]}
    assert got == {"tab_wasser": [keep[0]], "water_conditions": [keep[1]],
                   "water_service_prices": [keep[2]], "water_tariff": [keep[3]]}


def test_fact_rules_match_document_wording():
    import re
    from hnplan.hnvg_docs import RULES
    text = {
        "water_conditions": "Wasserzählerschrank anbringt, wenn die Länge der Anschlussleitung auf dem anzuschließenden "
                            "Grundstück 40 m überschreitet. 9.2 Die SWHN ist berechtigt als Messeinrichtung einen "
                            "fernauslesbaren Wasserzähler zu verwenden. vor. grundsätzlich jährlich im rollierenden Verfahren",
        "tab_wasser": "auf dem kürzesten Wege von der Versorgungsleitung 3. Wasserzählerbügel (so nah wie möglich im "
                      "Bereich der HAE) 10. Montagehöhe mindestens 600 mm",
        "water_service_prices": "a) Kontrollablesung auf Wunsch des Kunden (zusätzliche Abrechnung) 92,00 109,48 b) "
                                "Kontrollablesung auf Wunsch des Kunden (zusätzliche Abrechnung) Schacht 178,00 211,82",
        "water_tariff": "Der Mengenpreis für die Lieferung von Trinkwasser beträgt Euro/m³ 2,99 3,20",
    }
    vals = {}
    for r in RULES:
        m = re.search(r.pattern, text[r.dataset], re.I)
        assert m, r.key
        vals[r.key] = float(m.group(1).replace(",", ".")) if r.kind == "number" else True
    assert vals["shaft_pipe_length_m"] == 40 and vals["visit_cost_eur_net"] == 92
    assert vals["visit_cost_shaft_eur_net"] == 178 and vals["water_price_eur_m3_net"] == 2.99


def test_min_sites_reaches_target_and_is_minimal():
    import itertools

    from hnplan.optimize import Planner
    pl = Planner.__new__(Planner)
    rng = np.random.default_rng(7)
    pl.P = rng.uniform(0, 0.9, (80, 10)).astype(np.float32) * (rng.uniform(size=(80, 10)) > 0.6)
    pl.w = rng.integers(1, 20, 80).astype(float)
    pl.W = pl.w.sum()
    pl.existing, pl.candidates = [0], list(range(1, 10))
    pl.NEAR = (pl.P > 0.5).astype(np.float32)
    pl.sites = pd.DataFrame(dict(site_id=[f"s{i}" for i in range(10)], name=[f"s{i}" for i in range(10)]))
    best_all = pl._cov(pl.P, pl.existing + pl.candidates)
    for frac in (0.8, 0.9, 0.97):
        target = frac * best_all
        r = pl.min_sites(target)
        chosen = pl.site_index(r["sites"])
        assert r["reached"] and pl._cov(pl.P, pl.existing + chosen) >= target
        # brute force: no smaller set of candidates reaches the target
        smaller = any(pl._cov(pl.P, pl.existing + list(c)) >= target
                      for c in itertools.combinations(pl.candidates, r["k"] - 1))
        assert not smaller, (frac, r["k"])
    assert not pl.min_sites(best_all + 0.01)["reached"]


def test_meter_maker_flags_from_meter_number():
    from hnplan.model import add_device_flags, maker_of
    ids = pd.Series(["8APA0000000001", "8IMT0000000002", "8AXI0000000003"])
    assert maker_of(ids).tolist() == ["APA", "IMT", "AXI"]
    L = add_device_flags(pd.DataFrame(dict(zaehlernummer_id=ids)))
    assert L.is_apa.tolist() == [1.0, 0.0, 0.0] and L.is_axi.tolist() == [0.0, 0.0, 1.0]  # IMT = reference


def test_gateway_mounts_on_roof_not_tree():
    from hnplan.rasters import Grid
    from hnplan.sites import site_heights
    n = 40                                             # 200 m x 200 m at 5 m
    terrain = np.full((n, n), 100.0, np.float32)
    surface = terrain.copy()
    bheight = np.zeros((n, n), np.float32)
    tree = np.zeros((n, n), np.uint8)
    bheight[5:8, 5:8] = 12.0                                    # house: LoD height 12 m …
    surface[5:8, 5:8] = 112.0 + 3                               # … with a chimney in the surface model
    surface[9, 9] = 125.0                                       # tall tree next to it – must be ignored
    tree[9, 9] = 1
    surface[30, 30] = 140.0                                     # 40 m tower, no building around
    g = Grid(0.0, 200.0, 5.0, surface, terrain, bheight, np.zeros((n, n), np.uint8), "test", None, tree)
    sites = pd.DataFrame(dict(x=[32.5, 152.5, 100.0], y=[167.5, 47.5, 100.0]))
    s = site_heights(sites, g, dict(mast_m=2, roof_search_radius_m=30, site_search_radius_m=15,
                                    structure_min_m=8, free_mast_m=10))
    assert s["mount"].tolist() == ["roof", "structure", "free mast"]
    assert np.allclose(s["h_agl"], [14.0, 42.0, 10.0])       # roof 12 + 2 (no chimney, no tree), tower 40 + 2, pole 10


def test_lost_frames_from_frame_counter_gaps():
    from hnplan.weather import _lost_frames
    t0 = pd.Timestamp("2026-11-04T00:00Z")
    tx = pd.DataFrame(dict(id=["a", "b", "c", "d"], device_id="m1", fcnt=[1, 2, 5, 1000],
                           ts=[t0, t0 + pd.Timedelta(hours=1), t0 + pd.Timedelta(hours=4), t0 + pd.Timedelta(hours=5)]))
    lost = _lost_frames(tx)
    assert len(lost) == 2                                   # frames 3 and 4 missing; the jump to 1000 is a reset
    assert lost.ts.dt.floor("h").tolist() == [t0 + pd.Timedelta(hours=2), t0 + pd.Timedelta(hours=3)]


def test_xplan_parse_and_future_blocks():
    """XPlanGML (nested WFS collection, lat/lon axis order) -> zones; open farmland -> worst-case blocks + meters."""
    import io

    import geopandas as gpd
    from shapely.geometry import box

    from hnplan.future import build_future, future_cells, parse_xplan
    from hnplan.rasters import Grid

    gml = b"""<?xml version='1.0' encoding='UTF-8'?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2"
  xmlns:xplan="http://www.xplanung.de/xplangml/6/0" xmlns:xlink="http://www.w3.org/1999/xlink">
 <wfs:member><xplan:FP_Plan gml:id="FP_Plan_1"><xplan:name>FNP Test</xplan:name>
   <xplan:gemeinde><xplan:XP_Gemeinde><xplan:gemeindeName>Teststadt</xplan:gemeindeName></xplan:XP_Gemeinde></xplan:gemeinde>
   <xplan:bereich xlink:href="#FP_Bereich_1"/></xplan:FP_Plan></wfs:member>
 <wfs:member><wfs:SimpleFeatureCollection>
  <wfs:member><xplan:FP_BebauungsFlaeche gml:id="a1"><xplan:gehoertZuBereich xlink:href="#FP_Bereich_1"/>
   <xplan:position><gml:Polygon><gml:exterior><gml:LinearRing>
    <gml:posList>49.10 9.10 49.10 9.11 49.11 9.11 49.11 9.10 49.10 9.10</gml:posList>
   </gml:LinearRing></gml:exterior></gml:Polygon></xplan:position>
   <xplan:allgArtDerBaulNutzung>1000</xplan:allgArtDerBaulNutzung></xplan:FP_BebauungsFlaeche></wfs:member>
  <wfs:member><xplan:FP_BebauungsFlaeche gml:id="far"><xplan:position><gml:Polygon><gml:exterior><gml:LinearRing>
    <gml:posList>48.0 8.0 48.0 8.01 48.01 8.01 48.01 8.0 48.0 8.0</gml:posList>
   </gml:LinearRing></gml:exterior></gml:Polygon></xplan:position></xplan:FP_BebauungsFlaeche></wfs:member>
 </wfs:SimpleFeatureCollection></wfs:member>
</wfs:FeatureCollection>"""
    df = parse_xplan(io.BytesIO(gml), (9.0, 49.0, 9.2, 49.2))
    assert len(df) == 1 and df.plan.iloc[0] == "FNP Test" and df.gemeinde.iloc[0] == "Teststadt"
    minx, miny, maxx, maxy = df.geometry.iloc[0].bounds
    assert abs(minx - 9.10) < 1e-9 and abs(miny - 49.10) < 1e-9  # lat/lon swapped to lon/lat

    # 400 m x 400 m grid: a residential zone on farmland in the east half, existing 9 m buildings in the west
    n, res = 80, 5.0
    zero = np.zeros((n, n), np.float32)
    bh = zero.copy()
    bh[:, :20] = np.tile([9, 9, 0, 0], 5)[None, :]
    grid = Grid(0.0, 400.0, res, zero + bh, zero.copy(), bh, np.zeros((n, n), np.uint8), "test")
    zones = gpd.GeoDataFrame(dict(use=["residential"], ftype=["FP_BebauungsFlaeche"], plan=["FNP Test"], gemeinde=["T"],
                                  plan_id=["p"], area_m2=[1.0]), geometry=[box(200, 0, 400, 400)], crs="EPSG:25832")
    blds = gpd.GeoDataFrame(dict(height=np.r_[np.full(30, 9.0), np.full(10, 20.0)]),
                            geometry=[box(5 + 10 * (i % 5), 5 + 10 * (i // 5), 9 + 10 * (i % 5), 9 + 10 * (i // 5)) for i in range(40)],
                            crs="EPSG:25832")
    lu = gpd.GeoDataFrame(dict(nutzart=["Landwirtschaft"]), geometry=[box(150, 0, 400, 400)], crs="EPSG:25832")
    sites = pd.DataFrame(dict(x=[390.0], y=[390.0]))
    fg, fb, areas, summ = build_future(grid, zones, blds, lu, None, sites, {"gap_m": 25, "min_area_m2": 3000})
    assert summ["future_buildings"] > 20 and len(areas) == 1
    assert abs(fb.height.iloc[0] - np.quantile(blds.height, 0.9)) < 1e-6   # worst case = P90 of existing heights
    assert (fg.bheight[:, :20] == grid.bheight[:, :20]).all()             # existing city untouched
    assert fg.bheight[:, 40:].max() > 0                                    # planned blocks inside the zone
    r, c, _ = fg.rc(np.array([390.0]), np.array([390.0]))
    assert fg.bheight[r[0], c[0]] == 0                                     # keep-out around the gateway site
    fc = future_cells(fb, 50, 100)
    assert fc.n_addr.sum() == len(fb) and fc.cell_id.min() == 100


def test_city_candidates_and_config():
    """Other cities: highest roof per square with addresses becomes a site; operator datasets are not required."""
    import geopandas as gpd
    from shapely.geometry import box

    from hnplan.city import city_config, generate_candidates

    # two 400 m squares with addresses, one without; in square A the higher roof (ground + height) wins
    blds = gpd.GeoDataFrame(dict(height=[10.0, 12.0, 30.0, 25.0], ground_z=[300.0, 250.0, 200.0, 200.0]),
                            geometry=[box(10, 10, 30, 30), box(100, 100, 120, 120), box(500, 10, 520, 30), box(900, 10, 920, 30)],
                            crs="EPSG:25832")
    cells = pd.DataFrame(dict(x=[50.0, 450.0], y=[50.0, 50.0], n_addr=[40, 40]))
    ccfg = dict(spacing_m=400, min_roof_m=6, min_footprint_m2=60, min_addresses=5, prefix="T")
    s = generate_candidates(blds, cells, ccfg, "EPSG:25832")
    assert len(s) == 2 and list(s.site_id) == ["T001", "T002"]
    assert set(s.roof_h) == {10.0, 30.0}          # 300 + 10 beats 250 + 12; the square without addresses gets none
    assert (s.kind == "candidate").all() and s.lat.between(-90, 90).all()

    cfg = city_config("stuttgart") if (Path(__file__).resolve().parents[1] / "cities/stuttgart/raw").exists() else None
    if cfg is not None:
        assert "transmissions" not in cfg.datasets and cfg.raw["city"]["name"] == "Stuttgart"
        assert str(cfg.processed_dir).replace("\\", "/").endswith("cities/stuttgart/processed")
