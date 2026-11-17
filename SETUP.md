# Setting up the HN LoRaWAN Planner on another computer

This guide gets the planner running on a new Windows, macOS or Linux machine.
There are two ways:

- **A – Just run the app** (about 30 MB to copy, 5 minutes). Shows the 3D planner with the current results.
  Enough for demos and for planning sites.
- **B – Full rebuild** (about 12 GB of raw data, 15 minutes). Needed only to retrain the model or to add new data.

---

## 1. Install uv (once per computer)

`uv` installs the right Python version and all libraries by itself. You don't need to install Python separately.

**Windows** – open PowerShell and run:
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux** – open a terminal and run:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close the terminal and open a new one, then check:
```bash
uv --version
```

Optional: a current Chrome, Edge or Firefox for the app, and [Git](https://git-scm.com/) if you want version history.

---

## 2. Copy the project

### Option A – app only (small)

Copy the `lorawan-planner` folder **without** these parts:

| Leave out | Why |
|---|---|
| `.venv/` | gets recreated on the new computer |
| `data/raw/` | 12 GB of source data – only needed for option B |
| `data/processed/` except `cells.parquet`, `sites.parquet`, `predictions.parquet` | caches, rebuilt by option B |

It must contain `web/data/` (the app's data) and those three `.parquet` files (used by the planning API).

On Windows 10/11, open PowerShell **inside the project folder** and paste this. It creates
`lorawan-planner-app.zip` (about 26 MB) next to the project folder:
```powershell
$out = Split-Path $PWD; $stage = Join-Path $out "_pack\lorawan-planner"
robocopy . $stage /E /XD "$PWD\.venv" "$PWD\data" "$PWD\.ruff_cache" "$PWD\.pytest_cache" __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
New-Item -ItemType Directory -Force "$stage\data\processed", "$stage\data\raw" | Out-Null
Copy-Item data\processed\cells.parquet, data\processed\sites.parquet, data\processed\predictions.parquet "$stage\data\processed\"
Copy-Item data\raw\README.txt "$stage\data\raw\"
tar -a -c -f "$out\lorawan-planner-app.zip" -C "$out\_pack" lorawan-planner
Remove-Item -Recurse -Force "$out\_pack"
```
(`tar` writes a standard zip that also opens correctly on macOS and Linux – PowerShell's own
`Compress-Archive` does not.)

On the new computer, unzip `lorawan-planner-app.zip` anywhere. It contains one folder, `lorawan-planner`.

### Option B – full project (large)

Copy the whole folder except `.venv/`. Copy `data/raw/` as it is: the zips inside stay zipped.
An external drive is easiest for 12 GB.

> **Never unzip a newer project zip on top of an existing project folder.** It silently replaces your
> files with older versions. Unzip into a new folder instead, and ask Claude Code to merge the changes.

---

## 3. Install and start

Open a terminal **in the project folder** (on Windows: in Explorer, click the address bar, type `cmd`, press Enter):

```bash
uv sync
uv run hnplan serve
```

Open **http://localhost:8000** in the browser. Stop the server with `Ctrl + C`.

The first `uv sync` downloads Python 3.13 and the libraries (~400 MB, a few minutes). After that it's instant.

**Shortcut on Windows:** double-click **`start-planner.bat`** in the project folder. It runs both commands,
opens the browser by itself as soon as the planner is ready, and if the planner is already running it just
opens the browser. Close its window to stop the planner. Tip: right-click the file → *Send to → Desktop
(create shortcut)* to start it from the desktop. Port 8000 taken? Run `set PLANNER_PORT=8001` before starting it
from a terminal.

---

## 4. Full rebuild (option B only)

### Where the data goes

Everything goes into `data/raw/`. Zips stay zipped; file names may have prefixes such as `84cd939d-`.

| Data | Source | Required |
|---|---|---|
| `transmission_data.csv`, `transmitter meta data.csv`, `mapping transmitter id.csv`, `gateway meta data.csv`, `potential_gw_location.csv`, `HN_Adresses.csv` | HNVG (challenge) | yes |
| `ALKIS-oE_*_shp.zip` (6 districts) | LGL Open GeoData | yes |
| `LoD1_32_*` / `LoD2_32_*` 3D buildings | LGL Open GeoData | yes |
| `dom1_32_*` surface model | LGL Open GeoData | yes |
| `dgm1_32_*` terrain model | LGL Open GeoData | recommended |
| `dop20rgbi_32_*` orthophotos (**RGBI**, not RGB) | LGL Open GeoData | optional (aerial imagery) |
| `hk_bw.zip` Hauskoordinaten | LGL Open GeoData | optional (better geocoding) |
| `bdlm_bw_shp.zip` Basis-DLM | LGL Open GeoData | optional (forest, water) |
| 4 water PDFs in `data/raw/hnvg_docs/`: `tab-wasser.pdf`, `Ergaenzende-Bedingungen-AVBWasserV.pdf`, `Montagepreise-Wasserversorgung-Systemleistungen_HNVG.pdf`, `Preisblatt-AVB_WasserV.pdf` | HNVG website | optional (shaft flag, value line) |

Other files in `data/raw` (photos, other PDFs, extra layers) are ignored automatically.

### Run

```bash
uv run hnplan discover            # check: every required dataset should show ≥ 1 file
uv run hnplan all --force         # full rebuild, about 10–15 minutes
uv run python scripts/ablation_placement.py
uv run hnplan export
uv run hnplan serve
```

Use `--force` whenever you add or replace files in `data/raw`. Without it the program reuses its saved caches.
Run only one of these commands at a time.

---

## 5. Using the app

- **Plan for meter type:** *Today's mix* uses the meters as installed (IMT, Apator, Axioma). *Weakest* plans as if every
  meter were the type with the lowest reception (Apator) – the safe choice if future meters are unknown.
- **City:** *Today* = buildings and addresses as they are. *Future city* (shown when `xplan_*.gml` plans from
  geoportal-raumordnung-bw.de are in `data/raw`) adds the buildings the land-use plans allow on land that is still farmland,
  as obstacles at worst-case height and with one new meter each. Plan in *Future city* for a network that still holds when the
  new districts are built; the **Future-proof check** under the plan compares both.
- **Plan new gateways – Coverage target (default):** move the slider to the coverage you need (up to what all approved
  sites can reach) and click **Find minimum sites**. You get the fewest approved sites that reach it, in build order,
  with the gain of each site. The chart shows coverage against the number of new gateways.
- **Plan new gateways – Fixed number:** pick a goal (*Max coverage*, *300 m rule*, *Confident only*, *With backup*), set the
  number of sites with − / +, click **Find best sites**. Each result card lists addresses and measured meters within 300 m and the
  addresses newly covered according to the model. Click a card to fly to the site, × to remove it.
- **On the map:** white badge = existing gateway, small ring = candidate site, numbered blue hexagon = new gateway.
  Solid ring = 300 m device radius (provider rule); dashed ring and blue glow = where the model predicts ≥ 50 % reception.
- **Click any gateway** to switch it on or off (e.g. "what if this gateway fails?"). Hover a building for its numbers.
- **Map layers** switches hide or show parts of the map. Hidden existing gateways still count for coverage.
- **Methodology** (bottom of the panel) explains the data, the model and its limits.
- **Data ↗** (top of the panel) opens the *Data & evidence* page: all numbers as tables – sites, plan, model,
  measurements, meters and sources. Click a column to sort, search, and download any table as CSV.
- The address bar stores the current scenario, e.g. `http://localhost:8000/#k=3&goal=radius` – copy it to share a scenario.

---

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| `uv` is not recognised | Open a new terminal after installing uv. |
| `failed to remove file …hnplan.exe … used by another process` | The server is still running. Stop it (`Ctrl + C`), then repeat the command. |
| `Required dataset 'transmissions' not found` | The HNVG CSV files are missing from `data/raw` (section 4). |
| Page says *Could not load the planning data* | `web/data/` is missing or empty – copy it from the original computer, or run `uv run hnplan all`. |
| `address already in use` / port 8000 busy | `uv run hnplan serve --port 8001` and open http://localhost:8001 |
| Map background is black, no street names | The basemap needs internet. Everything else works offline. |
| Changes don't show in the browser | Reload with `Ctrl + F5`. |
| Aerial imagery switch is greyed out | No DOP20 tiles were exported – add `dop20rgbi_*` zips and run `uv run hnplan all --force`. |
| Text looks garbled in the Windows terminal | Run `set PYTHONIOENCODING=utf-8` (cmd) or `$env:PYTHONIOENCODING="utf-8"` (PowerShell) first. |

### Docker (alternative)

With Docker installed and `web/data` present:
```bash
docker build -t hnplan .
docker run -p 8000:8000 hnplan
```

---

## 7. Version control

It is strongly recommended to put the project under Git before making changes:
```bash
git add -A
git commit -m "Working state"
```
