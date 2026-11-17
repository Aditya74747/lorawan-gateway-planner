# Other cities

The Heilbronn planner, generalised: put a city's open data into `cities/<name>/raw/` and get the same 3D app
on its own localhost. **No operator data is needed.**

| | Heilbronn (`data/`) | Any other city (`cities/<name>/`) |
|---|---|---|
| Meters | HNVG address list + measured meters | every official house coordinate (LGL Hauskoordinaten) = one water meter |
| Gateway sites | HNVG's 71 approved candidates + 8 existing | **proposed automatically**: the highest roof in every 400 m square that has addresses (antenna roof + 2 m) |
| Existing gateways | 8 (HNVG) | none, or listed in `raw/gateways.csv` |
| Reception model | trained on the measured week | the same Heilbronn measurements, **transfer version without absolute ground elevation** (Heilbronn 157–218 m, Stuttgart 225–385 m; validation: new-gateway AUC 0.966 vs 0.969) |
| Planning | fewest approved sites for a target | fewest proposed rooftops for a target (2-for-1 swap step skipped above 100 candidates) |

## Run it

```bash
# once, in the main project (trains the Heilbronn model the cities use):
uv run hnplan all

uv run hnplan city stuttgart all                 # analyse cities/stuttgart/raw  (≈ 10 min for all of Stuttgart, --force after new files)
uv run hnplan city stuttgart serve --port 8001   # http://localhost:8001  (Heilbronn keeps running on 8000)
```

Or double-click **`start-city.bat`** (Stuttgart, port 8001), or `start-city.bat ulm 8002`.
Single steps: `prepare`, `features`, `predict`, `export`; add `--force` after changing files in `raw/`.

## New city in 4 steps

1. Copy `cities/_template` to `cities/<name>` (lower case, no spaces, e.g. `cities/ulm`) and set `city.name` in `city.yaml`.
2. Find the tile numbers. LGL tiles are named by UTM 32 kilometres of their lower-left corner, in 2 km steps:
   **east = odd number, north = even number** (e.g. `513_5402` = east 513–515 km, north 5402–5404 km).
   Convert the city centre with any lat/lon → UTM 32N converter (Stuttgart Schlossplatz: E 513.2 km, N 5402.8 km)
   and take a block of 3 × 3 tiles around it (6 × 6 km).
3. Download into `cities/<name>/raw/` (keep the zips – they are read in place) – see the list below.
4. `uv run hnplan city <name> all`, then `uv run hnplan city <name> serve --port 8002`.

## Files to download (LGL Open GeoData, https://opengeodata.lgl-bw.de, licence dl-de/by-2-0)

For every tile `E_N` of the area. Whole Stuttgart (in `cities/stuttgart/raw`): E = 505 … 521 and N = 5394 … 5412 → 90 tiles
per dataset (a 6 × 6 km test: E = 511, 513, 515 and N = 5400, 5402, 5404 → 9 tiles). **Check that no column is missing** –
`hnplan city <name> prepare` prints the tile range per dataset; a missing DOM1 column leaves a strip without addresses and obstacles.

| Required | File name | Direct link pattern | What for |
|---|---|---|---|
| ✔ | `LoD2_32_E_N_2_bw.zip` | `https://opengeodata.lgl-bw.de/data/lod2/LoD2_32_E_N_2_bw.zip` | 3D buildings: obstacles, roofs for gateways |
| ✔ | `dom1_32_E_N_2_bw.zip` | `https://opengeodata.lgl-bw.de/data/dom1/dom1_32_E_N_2_bw.zip` | surface model 1 m: line of sight incl. trees |
| ✔ (recommended) | `dgm1_32_E_N_2_bw.zip` | `https://opengeodata.lgl-bw.de/data/dgm/dgm1_32_E_N_2_bw.zip` | terrain 1 m (estimated from DOM1 if missing) |
| ✔ | `hk_bw.zip` (once, whole state) | Open GeoData portal → *Hauskoordinaten* | address points = water meters (same file as Heilbronn) |
| optional | `bdlm_bw_shp.zip` (once, whole state) | Open GeoData portal → *Basis-DLM* | forest and water |
| optional | `dop20rgbi_32_E_N_2_bw.zip` | Open GeoData portal → *DOP20 RGBI* | aerial photo layer (large: ~350 MB per tile) |
| optional | `gateways.csv` | your own file: `name;lat;lon` | gateways that already exist |

`hk_bw.zip` and `bdlm_bw_shp.zip` are state-wide: copy them from `data/raw/` instead of downloading again.
PowerShell one-liner for the 27 tile files of the Stuttgart block:

```powershell
cd cities\stuttgart\raw
foreach ($e in 511,513,515) { foreach ($n in 5400,5402,5404) {
  foreach ($p in "lod2/LoD2","dom1/dom1","dgm/dgm1") { $f = ($p -split "/")[1] + "_32_${e}_${n}_2_bw.zip"
    Invoke-WebRequest "https://opengeodata.lgl-bw.de/data/$p`_32_${e}_${n}_2_bw.zip" -OutFile $f } } }
```

Cities outside Baden-Württemberg: the same kinds of data exist as open data in most German states
(3D buildings LoD2, DOM/DGM, house coordinates); file name patterns are in `config.yaml → datasets`.

## Size settings (`city.yaml`)

The browser holds a cells × sites matrix. Rule of thumb: keep it ≤ ~10,000 cells × ~300 sites.
Central 6 × 6 km: `cell_size_m: 50`, `spacing_m: 400` (7,464 × 204). Whole Stuttgart: `cell_size_m: 100`, `spacing_m: 800`,
`max_range_m: 3000` (10,163 × 295 → 46 / 67 sites for 95 / 98 %). ALKIS files are not needed for a city (the house coordinates
already have positions); `municipalities` restricts the meters to one town when tiles reach into neighbours.

## Limits

* Without measurements the model is applied outside the place it was trained. About half of Stuttgart's radio paths
  lie partly outside the Heilbronn range (taller roofs, deeper valleys); the model then keeps the nearest known value and
  can never give *more* reception for more obstruction (monotone constraints).
* Every address is one basement meter; no shafts, no meter makers known (today's Heilbronn mix is assumed, or the weakest type).
* Real rollout: measure one week with 2–3 test gateways in the city and retrain – the pipeline is the same as for Heilbronn.
