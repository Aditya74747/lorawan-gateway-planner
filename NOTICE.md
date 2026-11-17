# Third-party notices and data licences

The code written by Team Odyssey is under the [MIT License](LICENSE). Everything listed here belongs to
its owners and is used under its own licence. **No raw data is stored in this repository.**

## Datasets

| Dataset | Owner / source | Licence | In this repo? |
|---|---|---|---|
| 3D buildings LoD1/LoD2, digital surface model DOM1, terrain model DGM1, orthophotos DOP20, ALKIS buildings and land use, Basis-DLM, house coordinates (Hauskoordinaten) | Landesamt für Geoinformation und Landentwicklung Baden-Württemberg (LGL), [LGL Open GeoData](https://www.lgl-bw.de/Produkte/Open-Data/) | [Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0)](https://www.govdata.de/dl-de/by-2-0) | No – downloaded by the user into `data/raw` |
| Land-use plans (Flächennutzungsplan) and regional plan (Regionalplan Heilbronn-Franken) as XPlanGML | [Geoportal Raumordnung Baden-Württemberg](https://www.geoportal-raumordnung-bw.de/) | Published free of charge by the State of Baden-Württemberg; the service metadata names dl-de/by-2-0 for comparable XPlanung services – check the terms of the individual service before reuse | No |
| LoRaWAN reception records, meter and gateway metadata, 71 candidate sites, water-supply documents | Heilbronner Versorgungs GmbH (HNVG), provided for the Future City Hackathon 2026 | Not open data – © HNVG. Must not be published without HNVG's permission | **No** |
| Historical weather (optional, `hnplan weather --fetch`) | [Open-Meteo.com](https://open-meteo.com/) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) – "Weather data by Open-Meteo.com" | No – fetched at run time |
| Gateway hardware values in `config.yaml` | Kerlink public product datasheet | Facts from public sources only | Values only |

**Attribution for maps and images produced with this software:**
Geodata © LGL Baden-Württemberg, [dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0) (data processed: 3D raster, line-of-sight analysis) ·
Basemap © [CARTO](https://carto.com/attributions) © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) ·
Reception model trained on measurements provided by HNVG.

The images and video in `docs/media/` are screenshots of our app. They contain these map data and show
model results; the attribution above applies to them.

## Map tiles and fonts (loaded at run time, not stored here)

| Item | Owner | Licence / terms |
|---|---|---|
| Dark Matter basemap style and vector tiles | [CARTO](https://carto.com/basemaps/) | [CARTO Basemap terms](https://carto.com/legal/basemap-terms/) – attribution required; heavy or commercial use needs a (free) CARTO API key |
| Map data inside the basemap | [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/) |
| Geist and Geist Mono fonts (via Google Fonts) | Vercel | [SIL Open Font License 1.1](https://openfontlicense.org/) |

## JavaScript libraries (included in `web/vendor/`)

| Library | Version | Licence | Licence file |
|---|---|---|---|
| [deck.gl](https://deck.gl/) | 9.4 | MIT | [web/vendor/LICENSE-deck.gl](web/vendor/LICENSE-deck.gl) |
| [MapLibre GL JS](https://maplibre.org/) | 4.7.1 | BSD 3-Clause | [web/vendor/LICENSE-maplibre.txt](web/vendor/LICENSE-maplibre.txt) |

## Python libraries (installed by `uv sync`, not included here)

| Library | Licence |
|---|---|
| CatBoost | Apache 2.0 |
| FastAPI | MIT |
| GeoPandas | BSD 3-Clause |
| lxml | BSD 3-Clause |
| NumPy | BSD 3-Clause |
| pandas | BSD 3-Clause |
| Pillow | MIT-CMU (HPND) |
| PyArrow | Apache 2.0 |
| pyogrio | MIT |
| pypdf | BSD 3-Clause |
| pyproj | MIT |
| PyYAML | MIT |
| rasterio | BSD 3-Clause |
| Requests | Apache 2.0 |
| scikit-learn | BSD 3-Clause |
| SciPy | BSD 3-Clause |
| Shapely | BSD 3-Clause |
| Uvicorn | BSD 3-Clause |
| LightGBM / XGBoost (optional `bench` group) | MIT / Apache 2.0 |
| pytest (dev) | MIT |

All of these are permissive licences that allow use in an MIT-licensed project.
