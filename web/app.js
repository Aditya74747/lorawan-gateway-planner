/* Heilbronn LoRaWAN Planner – 3D browser app (deck.gl + MapLibre, no build step).
   All planning maths runs in the browser on the exported reception matrix:
   P[cell, site] = model probability that a gateway at `site` hears a device in `cell`. */
(async function () {
  const $ = (id) => document.getElementById(id);
  const setStep = (html) => { $("loading-step").innerHTML = html; };
  const tick = () => new Promise((r) => setTimeout(r, 0)); // let the browser paint the loading text

  // ---------- data ----------
  let sites, C, B, meters, INS;
  try {
    const load = async (n) => {
      const r = await fetch(`data/${n}.json`);
      if (!r.ok) throw new Error(`data/${n}.json – HTTP ${r.status}`);
      return r.json();
    };
    [sites, C, B, meters, INS] = await Promise.all(["sites", "cells", "buildings", "meters", "insights"].map(load));
  } catch (e) {
    document.querySelector(".spinner").style.display = "none";
    setStep(`Could not load the planning data (${e.message}).<br>Run <code>uv run hnplan all</code>, then reload this page.`);
    return;
  }
  // other cities (hnplan city <name> serve): same app, no operator data, generated rooftop candidates
  const CITY = INS.city || null;
  const CITYNAME = CITY ? CITY.name : "Heilbronn";
  const SITEWORD = CITY ? "rooftop sites" : "approved sites";
  if (CITY) {
    document.title = `${CITYNAME} LoRaWAN Planner`;
    document.querySelectorAll("h1, .loading-title").forEach((e) => { e.textContent = `${CITYNAME} LoRaWAN Planner`; });
    $("map").setAttribute("aria-label", `3D map of ${CITYNAME}`);
    $("plan-empty").textContent = `No operator data here: the planner proposed ${CITY.candidates} rooftop sites (highest roof per ` +
      `${CITY.spacing_m} m square). Set a coverage target and find the fewest of them – or click a grey site to test it.`;
    const cr = document.querySelector(".credits");
    if (cr) cr.textContent = "Geodata: LGL Baden-Württemberg (dl-de/by-2-0) · Reception model trained on HNVG's Heilbronn measurements · Basemap © CARTO © OpenStreetMap";
  }
  // optional aerial imagery (DOP20) – absent when no DOP20 tiles were supplied
  const VEG = await fetch("data/vegetation.json").then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const aerial = (VEG && VEG.aerial) || [];
  // optional future city scenario (planned buildings from the land-use plans) – absent without XPlanung data
  const FUT = await fetch("data/future.json").then((r) => (r.ok ? r.json() : null)).catch(() => null);
  setStep("Decoding reception predictions…");
  await tick();

  // ---------- decode matrices ----------
  const b64 = (s) => Uint8Array.from(atob(s), (ch) => ch.charCodeAt(0));
  const nS = C.n_sites;
  let nC = C.n_cells; // cells of the active city scenario (future city = today's cells + new cells appended)
  // probabilities come with 16 bits (p16, current exports) or 8 bits (p, older exports)
  const decode = (n, sp16, sp8, ss) => {
    const su = b64(ss), p = new Float32Array(n * nS), sd = new Float32Array(n * nS);
    if (sp16) { const u = new Uint16Array(b64(sp16).buffer); for (let i = 0; i < n * nS; i++) p[i] = u[i] / 65535; }
    else { const u = b64(sp8); for (let i = 0; i < n * nS; i++) p[i] = u[i] / 255; }
    for (let i = 0; i < n * nS; i++) sd[i] = su[i] / 510;
    return { p, sd };
  };
  const decRssi = (n, s) => {
    const u = b64(s), r = new Float32Array(n * nS);
    for (let i = 0; i < n * nS; i++) r[i] = u[i] ? u[i] - 160 : NaN;
    return r;
  };
  // city scenarios, each with two meter scenarios: today's manufacturer mix, and every meter like the weakest type
  const SCN = { today: { nC, CL: { lon: C.lon, lat: C.lat, n: C.n, ex: C.ex }, SH: C.shaft || new Array(nC).fill(0),
    DEV: { mix: decode(nC, C.p16, C.p, C.std) }, RS: decRssi(nC, C.rssi) } };
  if (C.p_weak16 || C.p_weak) SCN.today.DEV.weak = decode(nC, C.p_weak16, C.p_weak, C.std_weak);
  if (FUT && FUT.n_today === C.n_cells) {
    const n = FUT.n_cells;
    SCN.future = { nC: n, CL: { lon: C.lon.concat(FUT.lon), lat: C.lat.concat(FUT.lat), n: C.n.concat(FUT.n), ex: C.ex.concat(FUT.ex) },
      SH: SCN.today.SH.concat(new Array(n - nC).fill(0)), DEV: { mix: decode(n, FUT.p16, null, FUT.std) }, RS: decRssi(n, FUT.rssi) };
    if (SCN.today.DEV.weak && FUT.p_weak16) SCN.future.DEV.weak = decode(n, FUT.p_weak16, null, FUT.std_weak);
  }
  for (const k in SCN) { const B_ = SCN[k]; B_.W = Float64Array.from(B_.CL.n); B_.WT = B_.W.reduce((a, b) => a + b, 0); B_.SH = Float32Array.from(B_.SH); }
  let CL = SCN.today.CL, DEV = SCN.today.DEV, RS = SCN.today.RS;
  const DEVINFO = C.devices || {};
  let P = DEV.mix.p, SD = DEV.mix.sd;
  // pessimistic probability (one model standard deviation lower) for the "confident only" goal
  let PLO = P.map((p, i) => Math.max(0, p - SD[i]));
  const COV_P = 0.5; // a cell counts as "reached" by a site at P >= 50 %
  const DEV_R = C.device_radius_m || 300; // challenge provider: device radius in metres
  let W = SCN.today.W, SH = SCN.today.SH, WT = SCN.today.WT;
  const BC = INS.business_case || { visit_cost: 92, visit_cost_shaft: 178 };
  const existing = sites.filter((s) => s.kind === "existing").map((s) => s.i);
  const candidates = sites.filter((s) => s.kind === "candidate").map((s) => s.i);

  // ---------- buildings ----------
  setStep("Building the 3D city…");
  await tick();
  const blds = [];
  for (let i = 0; i < B.h.length; i++) {
    const ring = [];
    for (let j = B.off[i]; j < B.off[i + 1]; j++) ring.push([B.lon0 + B.xy[2 * j] * B.scale, B.lat0 + B.xy[2 * j + 1] * B.scale]);
    // neutral mode: cool light greys with slight per-building variation, taller blocks a touch darker
    const v = ((i * 2654435761) >>> 0) % 18, t = Math.min(B.h[i] / 45, 1);
    const nat = [Math.round(176 + v - 30 * t), Math.round(182 + v - 28 * t), Math.round(192 + v - 24 * t)];
    blds.push({ ring, h: B.h[i], cell: B.cell[i], nat });
  }
  // landmarks (config.yaml -> landmarks, e.g. Hochschule Heilbronn): highlighted for the first seconds after opening
  const hexRGB = (h) => [1, 3, 5].map((i) => parseInt(String(h).slice(i, i + 2), 16));
  const LM = (B.landmarks || []).map((l) => ({ ...l, rgb: hexRGB(l.color),
    blds: l.idx.filter((i) => i < blds.length).map((i) => blds[i]) })).filter((l) => l.blds.length);
  const cellHasBld = new Uint8Array(nC);
  blds.forEach((b) => { if (b.cell >= 0) cellHasBld[b.cell] = 1; });
  // planned buildings of the future city (worst-case height), coloured by their own (new) cell
  const fblds = [];
  if (SCN.future) {
    const FB = FUT.buildings;
    for (let i = 0; i < FB.h.length; i++) {
      const ring = [];
      for (let j = FB.off[i]; j < FB.off[i + 1]; j++) ring.push([FB.lon0 + FB.xy[2 * j] * FB.scale, FB.lat0 + FB.xy[2 * j + 1] * FB.scale]);
      fblds.push({ ring, h: FB.h[i], cell: FB.cell[i], area: FB.area[i], use: FB.use[i] });
    }
  }
  const futAreas = SCN.future ? FUT.areas.flatMap((a) => a.rings.map((r) => ({ a, path: r }))) : [];
  const areaById = SCN.future ? Object.fromEntries(FUT.areas.map((a) => [a.id, a])) : {};
  for (const k in SCN) {
    const B_ = SCN[k];
    B_.cellPts = Array.from({ length: B_.nC }, (_, c) => ({ c, pos: [B_.CL.lon[c], B_.CL.lat[c]] }));
    B_.flatCells = B_.cellPts.filter((d) => d.c < C.n_cells && !cellHasBld[d.c]); // new cells always have a planned building
  }
  let cellPts = SCN.today.cellPts, flatCells = SCN.today.flatCells;

  // ---------- distances (metres, local flat approximation – fine at city scale) ----------
  const KX = 111320 * Math.cos((49.14 * Math.PI) / 180), KY = 110540;
  const dist = (lon1, lat1, lon2, lat2) => Math.hypot((lon2 - lon1) * KX, (lat2 - lat1) * KY);
  const circle = (lon, lat, r, n = 96) => Array.from({ length: n + 1 }, (_, k) => {
    const a = (2 * Math.PI * k) / n;
    return [lon + (r * Math.cos(a)) / KX, lat + (r * Math.sin(a)) / KY];
  });
  // provider rule as a 0/1 "probability": cell within DEV_R of the site
  for (const k in SCN) {
    const B_ = SCN[k];
    B_.NEAR = new Float32Array(B_.nC * nS);
    for (let c = 0; c < B_.nC; c++) for (let s = 0; s < nS; s++)
      if (dist(B_.CL.lon[c], B_.CL.lat[c], sites[s].lon, sites[s].lat) <= DEV_R) B_.NEAR[c * nS + s] = 1;
  }
  let NEAR = SCN.today.NEAR;
  const withinRadius = (act) => {
    let a = 0;
    for (let c = 0; c < nC; c++) for (const s of act) if (NEAR[c * nS + s]) { a += W[c]; break; }
    return a / WT;
  };
  // each measured meter -> the planning cell it lies in (nearest cell point within 60 m)
  const meterCell = meters.map((m) => {
    let best = -1, bd = 60;
    for (let c = 0; c < nC; c++) {
      if (Math.abs(C.lat[c] - m.lat) > 0.0006) continue;
      const d = dist(m.lon, m.lat, C.lon[c], C.lat[c]);
      if (d < bd) { bd = d; best = c; }
    }
    return best;
  });

  // ---------- state ----------
  const S = { disabled: new Set(), extra: new Set(), plan: [], mode: "cov", goal: "coverage", k: 3,
    pm: "target", target: 0.98, planSteps: [], planInfo: null, device: "mix", scen: "today",
    showB: true, showCand: true, showMet: false, showExist: true, showCov: true, showAer: false,
    hoverCell: -1, ver: 0, cov: [], t: 0, lm: 0, lmT: 0, grow: 1 };
  const active = () => existing.filter((i) => !S.disabled.has(i)).concat([...S.extra]);

  function compute(act) {
    const q0 = new Float64Array(nC).fill(1), q1 = new Float64Array(nC), v = new Float64Array(nC);
    const best = new Float32Array(nC).fill(NaN), bestS = new Int16Array(nC).fill(-1);
    for (let c = 0; c < nC; c++) {
      const row = c * nS;
      for (const s of act) {
        const p = Math.min(P[row + s], 0.999);
        if (p <= 0) continue;
        q1[c] = q1[c] * (1 - p) + q0[c] * p;
        q0[c] *= 1 - p;
        const r = RS[row + s];
        if (p > 0.05 && !(r <= best[c])) { best[c] = r; bestS[c] = s; }
      }
      for (const s of act) {
        const p = Math.min(P[row + s], 0.999);
        if (p <= 0) continue;
        const g = (q0[c] / (1 - p)) * SD[row + s];
        v[c] += g * g;
      }
    }
    const cov = new Float32Array(nC), red = new Float32Array(nC), unc = new Float32Array(nC);
    let sc = 0, sr = 0, gaps = 0, eur = 0;
    for (let c = 0; c < nC; c++) {
      cov[c] = 1 - q0[c]; red[c] = Math.max(0, 1 - q0[c] - q1[c]); unc[c] = Math.sqrt(v[c]);
      sc += W[c] * cov[c]; sr += W[c] * red[c]; if (cov[c] < 0.5) gaps += W[c];
      eur += W[c] * cov[c] * (BC.visit_cost * (1 - SH[c]) + BC.visit_cost_shaft * SH[c]);
    }
    return { cov, red, unc, best, bestS, q0, q1, summary: { cov: sc / WT, red: sr / WT, gaps, readable: sc, eur } };
  }

  function greedy(k, objective) {
    // "robust": plan with P minus one model standard deviation -> prefers sites whose
    // predicted coverage is also certain (well inside the training data)
    // "radius": provider rule – maximise addresses within DEV_R of a gateway (0/1 reach)
    const PP = objective === "robust" ? PLO : objective === "radius" ? NEAR : P;
    const act = existing.filter((i) => !S.disabled.has(i));
    const q0 = new Float64Array(nC).fill(1), q1 = new Float64Array(nC);
    for (let c = 0; c < nC; c++) for (const s of act) {
      const p = Math.min(PP[c * nS + s], 0.999);
      q1[c] = q1[c] * (1 - p) + q0[c] * p; q0[c] *= 1 - p;
    }
    const chosen = [];
    for (let step = 0; step < k; step++) {
      let bestS = -1, bestG = -1;
      for (const s of candidates) {
        if (chosen.includes(s)) continue;
        let g = 0;
        for (let c = 0; c < nC; c++) {
          const p = Math.min(PP[c * nS + s], 0.999);
          if (p <= 0) continue;
          const n0 = q0[c] * (1 - p), n1 = q1[c] * (1 - p) + q0[c] * p;
          const dcov = q0[c] - n0;
          const dred = (q0[c] + q1[c]) - (n0 + n1);
          g += W[c] * (objective === "redundancy" ? 0.5 * dcov + 0.5 * dred : dcov);
        }
        if (g > bestG) { bestG = g; bestS = s; }
      }
      if (bestS < 0) break;
      for (let c = 0; c < nC; c++) {
        const p = Math.min(PP[c * nS + bestS], 0.999);
        q1[c] = q1[c] * (1 - p) + q0[c] * p; q0[c] *= 1 - p;
      }
      chosen.push(bestS);
    }
    return chosen;
  }

  // ---------- fewest approved sites for a coverage target (same algorithm as Planner.min_sites) ----------
  const baseSites = () => existing.filter((i) => !S.disabled.has(i));
  function missOf(cols) { // per cell: probability that none of `cols` hears it
    const q0 = new Float64Array(nC).fill(1);
    for (let c = 0; c < nC; c++) { const row = c * nS; let q = 1; for (const s of cols) q *= 1 - P[row + s]; q0[c] = q; }
    return q0;
  }
  const covFromMiss = (q0) => { let a = 0; for (let c = 0; c < nC; c++) a += W[c] * (1 - q0[c]); return a / WT; };
  const covOf = (cols) => covFromMiss(missOf(cols));
  // greedy order of `pool` on top of `base`; returns order + coverage after each added site
  function orderGreedy(base, pool) {
    const q0 = missOf(base), left = new Set(pool), order = [], curve = [];
    const cov0 = covFromMiss(q0);
    const gainOf = (s) => { let g = 0; for (let c = 0; c < nC; c++) g += W[c] * q0[c] * P[c * nS + s]; return g; };
    if (pool.length > SWAP_MAX) {
      // lazy greedy (same order): a site's gain can only shrink as others are added (submodular), so a stale
      // gain is an upper bound – only the top of the list has to be re-evaluated (other cities, ~300 sites)
      let H = pool.map((s, i) => ({ s, i, g: gainOf(s), fresh: 0 }));
      for (let step = 1; H.length; step++) {
        for (;;) {
          H.sort((a, b) => b.g - a.g || a.i - b.i);
          if (H[0].fresh === step) break;
          H[0].g = gainOf(H[0].s); H[0].fresh = step;
        }
        const best = H.shift().s;
        order.push(best);
        for (let c = 0; c < nC; c++) q0[c] *= 1 - P[c * nS + best];
        curve.push(covFromMiss(q0));
      }
      return { order, cov0, curve };
    }
    while (left.size) {
      let best = -1, bg = -1;
      for (const s of left) {
        let g = 0;
        for (let c = 0; c < nC; c++) g += W[c] * q0[c] * P[c * nS + s];
        if (g > bg) { bg = g; best = s; }
      }
      left.delete(best); order.push(best);
      for (let c = 0; c < nC; c++) q0[c] *= 1 - P[c * nS + best];
      curve.push(covFromMiss(q0));
    }
    return { order, cov0, curve };
  }
  function prune(base, chosen, target) {
    chosen = [...chosen];
    while (chosen.length > 1) {
      let drop = -1, bestCov = -1;
      for (const i of chosen) {
        const cv = covOf(base.concat(chosen.filter((c) => c !== i)));
        if (cv > bestCov) { bestCov = cv; drop = i; }
      }
      if (bestCov < target) break;
      chosen = chosen.filter((c) => c !== drop);
    }
    return chosen;
  }
  function swap2for1(base, chosen, target) { // replace two sites by one unused site if the target holds
    const out = candidates.filter((c) => !chosen.includes(c));
    const rev = [...chosen].reverse();
    for (let x = 0; x < rev.length; x++) for (let y = x + 1; y < rev.length; y++) {
      const rest = chosen.filter((c) => c !== rev[x] && c !== rev[y]);
      const q0 = missOf(base.concat(rest));
      let wq = 0; for (let c = 0; c < nC; c++) wq += W[c] * q0[c];
      for (const o of out) {
        let m = 0;
        for (let c = 0; c < nC; c++) m += W[c] * q0[c] * (1 - P[c * nS + o]);
        if (1 - m / WT >= target) return rest.concat([o]);
      }
    }
    return null;
  }
  let curveCache = null;
  function coverageCurve() { // all candidates in greedy order (cached per set of switched-off gateways)
    const key = [...S.disabled].sort().join(",");
    if (!curveCache || curveCache.key !== key) curveCache = { key, ...orderGreedy(baseSites(), candidates) };
    return curveCache;
  }
  const SWAP_MAX = 100; // keep in sync with optimize.SWAP_MAX_CANDIDATES
  function minSites(target) {
    const base = baseSites(), G = coverageCurve();
    const k = G.curve.findIndex((c) => c >= target) + 1;
    let chosen = k > 0 ? G.order.slice(0, k) : [...G.order], swaps = 0;
    if (k > 0) {
      chosen = prune(base, chosen, target);
      // 2-for-1 swaps cost O(k² · candidates · cells): skipped for large generated candidate sets (same rule as optimize.py)
      for (let better; candidates.length <= SWAP_MAX && (better = swap2for1(base, chosen, target)); swaps++) chosen = prune(base, better, target);
    }
    const fin = orderGreedy(base, chosen);
    return { reached: k > 0, k: chosen.length, greedyK: k || null, swaps, order: fin.order, steps: fin.curve,
      cov0: G.cov0, max: G.curve[G.curve.length - 1] ?? G.cov0 };
  }

  // ---------- colours ----------
  const ACC = [76, 141, 255];
  const lerp = (a, b, t) => a.map((x, i) => Math.round(x + (b[i] - x) * t));
  const ramp = (stops) => (t) => {
    if (!(t >= stops[0][0])) return stops[0][1];
    for (let i = 1; i < stops.length; i++) if (t <= stops[i][0]) {
      const [t0, c0] = stops[i - 1], [t1, c1] = stops[i];
      return lerp(c0, c1, (t - t0) / (t1 - t0));
    }
    return stops[stops.length - 1][1];
  };
  const CORAL = [239, 83, 80], AMBER = [244, 163, 64], SAND = [222, 224, 180], TEAL = [61, 196, 160];
  const RAMPS = {
    cov: { f: ramp([[0, CORAL], [0.45, AMBER], [0.75, SAND], [1, TEAL]]), lo: 0, hi: 1, l: "0 %", r: "100 % chance of reception" },
    red: { f: ramp([[0, CORAL], [0.3, AMBER], [0.6, SAND], [1, TEAL]]), lo: 0, hi: 1, l: "no backup", r: "≥ 2 gateways" },
    rssi: { f: ramp([[-128, CORAL], [-116, AMBER], [-106, SAND], [-96, TEAL], [-85, ACC]]), lo: -128, hi: -85, l: "−128 dBm", r: "−85 dBm" },
    unc: { f: ramp([[0, [62, 70, 86]], [0.08, [100, 116, 168]], [0.18, [170, 116, 224]], [0.3, [236, 124, 250]]]), lo: 0, hi: 0.3,
      l: "model certain", r: "uncertain – measure here" },
  };
  const GREY = [48, 53, 62];
  const eur = (x) => (x >= 1e6 ? "€" + (x / 1e6).toFixed(2) + "M" : "€" + Math.round(x / 1e3).toLocaleString("en-US") + "k");
  const fmtN = (x) => Math.round(x).toLocaleString("en-US");
  const pct = (x, d = 1) => (100 * x).toFixed(d) + " %";
  let R = compute(active());
  let BASE_R = compute(existing), BASE = BASE_R.summary;
  let BASE_RAD = withinRadius(existing);

  // ---------- coverage footprint of every NEW (candidate) site that is switched on ----------
  // reached cells = P >= 50 %; radius = 90th percentile distance of reached cells (robust to outliers)
  function newSiteCoverage() {
    return [...S.extra].map((s) => {
      const st = sites[s], cells = [], d = [];
      let addr = 0, addrNew = 0;
      for (let c = 0; c < nC; c++) {
        const p = P[c * nS + s];
        if (p < COV_P) continue;
        cells.push({ c, p, pos: [CL.lon[c], CL.lat[c], 1] });
        d.push(dist(st.lon, st.lat, CL.lon[c], CL.lat[c]));
        addr += W[c];
        if (BASE_R.cov[c] < COV_P) addrNew += W[c];
      }
      d.sort((a, b) => a - b);
      const radius = d.length ? d[Math.floor(0.9 * (d.length - 1))] : 0;
      let met = 0, metNew = 0, met300 = 0, addr300 = 0;
      meters.forEach((m, i) => {
        const c = meterCell[i];
        if (c >= 0 && P[c * nS + s] >= COV_P) { met++; if (BASE_R.cov[c] < COV_P) metNew++; }
        if (dist(m.lon, m.lat, st.lon, st.lat) <= DEV_R) met300++;
      });
      for (let c = 0; c < nC; c++) if (NEAR[c * nS + s]) addr300 += W[c];
      return { s, cells, radius, addr, addrNew, met, metNew, met300, addr300 };
    });
  }
  const covBySite = () => new Map(S.cov.map((x) => [x.s, x]));

  function cellColor(c) {
    if (c < 0) return GREY;
    const m = S.mode;
    if (m === "real") return [150, 156, 166];
    if (m === "cov") return RAMPS.cov.f(R.cov[c]);
    if (m === "red") return RAMPS.red.f(R.red[c]);
    if (m === "unc") return RAMPS.unc.f(R.unc[c]);
    const r = R.best[c];
    return Number.isNaN(r) ? [110, 44, 52] : RAMPS.rssi.f(r);
  }

  // ---------- gateway symbols: a thin mast (real antenna height, exaggerated) topped by an icon ----------
  const svg = (s) => "data:image/svg+xml;charset=utf-8," + encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">${s}</svg>`);
  const ANT = (c) => `<path d="M32 46V31" stroke="${c}" stroke-width="4" stroke-linecap="round"/><circle cx="32" cy="27" r="4.2" fill="${c}"/>
    <path d="M22.5 18.5a13 13 0 0 0 0 17M41.5 18.5a13 13 0 0 1 0 17" stroke="${c}" stroke-width="3.6" fill="none" stroke-linecap="round"/>`;
  const HEX = "M32 4 56.2 18v28L32 60 7.8 46V18z";
  const ICON = {
    exist: svg(`<circle cx="32" cy="32" r="25" fill="#eceef1" stroke="#08090b" stroke-width="4"/>${ANT("#08090b")}`),
    existOff: svg(`<circle cx="32" cy="32" r="25" fill="#2a1517" stroke="#ff6b6b" stroke-width="4"/>${ANT("#ff6b6b")}
      <path d="M17 17l30 30" stroke="#ff6b6b" stroke-width="4" stroke-linecap="round"/>`),
    cand: svg(`<circle cx="32" cy="32" r="15" fill="#0b0d10" fill-opacity=".9" stroke="#b4bbc5" stroke-width="6"/>`),
    candLow: svg(`<circle cx="32" cy="32" r="15" fill="#0b0d10" fill-opacity=".8" stroke="#6b717b" stroke-width="5" stroke-dasharray="7 5"/>`),
    manual: svg(`<path d="${HEX}" fill="#4c8dff" stroke="#fff" stroke-width="3.5" stroke-linejoin="round"/>
      <path d="M32 21v22M21 32h22" stroke="#fff" stroke-width="5" stroke-linecap="round"/>`),
  };
  const MAX_RANK = candidates.length; // a numbered badge for every possible build position
  for (let k = 1; k <= MAX_RANK; k++) {
    ICON["rank" + k] = svg(`<path d="${HEX}" fill="#4c8dff" stroke="#fff" stroke-width="3.5" stroke-linejoin="round"/>
      <text x="32" y="42" text-anchor="middle" font-family="Arial,Helvetica,sans-serif" font-size="${k > 9 ? 22 : 27}" font-weight="700" fill="#fff">${k}</text>`);
  }
  $("mk-new").src = ICON.rank1; $("mk-exist").src = ICON.exist; $("mk-cand").src = ICON.cand;
  // All symbols are drawn once into one icon sheet (atlas) up front: deck.gl's on-demand icon loading
  // could miss icons that first appear after the initial render (e.g. rank badges after planning).
  async function buildAtlas() {
    const names = Object.keys(ICON), S2 = 64, cols = 8;
    const cv = document.createElement("canvas");
    cv.width = cols * S2; cv.height = Math.ceil(names.length / cols) * S2;
    const ctx = cv.getContext("2d"), mapping = {};
    await Promise.all(names.map((n, i) => new Promise((res) => {
      const img = new Image();
      img.onload = () => { ctx.drawImage(img, (i % cols) * S2, Math.floor(i / cols) * S2, S2, S2); res(); };
      img.onerror = res;
      img.src = ICON[n];
    })));
    names.forEach((n, i) => { mapping[n] = { x: (i % cols) * S2, y: Math.floor(i / cols) * S2, width: S2, height: S2, anchorY: S2 / 2 }; });
    return { atlas: cv.toDataURL("image/png"), mapping };
  }
  const ATLAS = await buildAtlas();
  const EX = 3; // vertical exaggeration of antenna masts
  const topZ = (s) => Math.max(s.h, 3) * EX;

  function glyph(s, act) {
    const pos = [s.lon, s.lat], top = [s.lon, s.lat, topZ(s)];
    if (s.kind === "existing") {
      const on = !S.disabled.has(s.i);
      // existing gateways: large badge + bright mast so they are visible without zooming in
      return { s, pos, top, icon: on ? "exist" : "existOff", size: 38, mast: on ? [255, 255, 255, 230] : [255, 107, 107, 220], w: 3 };
    }
    if (act.has(s.i)) {
      const k = S.plan.indexOf(s.i);
      return { s, pos, top, icon: k >= 0 && k < MAX_RANK ? "rank" + (k + 1) : "manual", size: 36, mast: [...ACC, 255], w: 3 };
    }
    return { s, pos, top, icon: s.conf === "low" ? "candLow" : "cand", size: 15,
      mast: s.conf === "low" ? [120, 126, 136, 60] : [180, 187, 197, 95], w: 1.5 };
  }

  // ---------- cached layer data (rebuilt only when the scenario or a switch changes) ----------
  const V = {};
  function buildView() {
    const act = new Set(active());
    const shown = sites.filter((s) => (s.kind === "existing" ? S.showExist : S.showCand || act.has(s.i)));
    // draw order: idle candidates first, then existing, then new sites on top
    const order = (g) => (g.s.kind === "existing" ? 1 : act.has(g.s.i) ? 2 : 0);
    V.act = act;
    V.glyphs = shown.map((s) => glyph(s, act)).sort((a, b) => order(a) - order(b));
    V.newCells = S.cov.flatMap((x) => x.cells);
    // with many new sites (target plans) keep the map calm: no labels, pulses or reach outlines
    const few = S.cov.length <= 8;
    V.reach = few ? S.cov.map((x) => ({ path: circle(sites[x.s].lon, sites[x.s].lat, Math.max(x.radius, 60)) })) : [];
    V.ruleRings = S.cov.map((x) => ({ pos: [sites[x.s].lon, sites[x.s].lat] }));
    V.pulse = few ? S.cov.flatMap((x) => [0, 1 / 3, 2 / 3].map((ph) => ({ pos: [sites[x.s].lon, sites[x.s].lat], ph }))) : [];
    V.labels = few ? S.cov.map((x) => ({ pos: [sites[x.s].lon, sites[x.s].lat, topZ(sites[x.s])], text: sites[x.s].id })) : [];
    // existing gateways: ground halo + short name label (e.g. "Heilbronn WT Böckingen" -> "WT Böckingen")
    const shortName = (n) => String(n).replace(/^Heilbronn[-\s]+/, "");
    // gateways closer than 1.2 km to another one get their label beside the symbol, pointing away from
    // the nearest neighbour (e.g. the three Klingenberg sites), so no name hides another
    const label = (s) => {
      let near = null, nd = 1200;
      for (const j of existing) {
        const o = sites[j];
        if (o.i === s.i) continue;
        const d = dist(s.lon, s.lat, o.lon, o.lat);
        if (d < nd) { nd = d; near = o; }
      }
      if (!near) return { anchor: "middle", off: [0, -33] };
      return s.lon >= near.lon ? { anchor: "start", off: [24, -6] } : { anchor: "end", off: [-24, -6] };
    };
    V.existing = S.showExist ? existing.map((i) => sites[i]).map((s) => ({ s, pos: [s.lon, s.lat],
      top: [s.lon, s.lat, topZ(s)], on: !S.disabled.has(s.i), text: shortName(s.name), ...label(s) })) : [];
    V.newCovered = new Set();
    meters.forEach((m, i) => { const c = meterCell[i]; if (c >= 0) for (const s of S.extra) if (P[c * nS + s] >= COV_P) V.newCovered.add(i); });
  }

  // ---------- map ----------
  setStep("Loading basemap…");
  async function pickStyle() {
    try {
      const ctl = new AbortController(); setTimeout(() => ctl.abort(), 4000);
      const r = await fetch("https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json", { signal: ctl.signal });
      if (r.ok) return await r.json();
    } catch (e) { /* offline -> plain background */ }
    return { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#08090b" } }] };
  }
  const wide = window.innerWidth > 900;
  // start view frames all 8 existing gateways (Klingenberg in the west … Schweinsberg in the south-east)
  const HOME = CITY && CITY.home ? { center: CITY.home.center, zoom: wide ? CITY.home.zoom : CITY.home.zoom - 0.5, pitch: 52, bearing: -18 }
    : { center: wide ? [9.183, 49.122] : [9.2, 49.13], zoom: wide ? 12.35 : 11.9, pitch: 52, bearing: -18 };
  const map = new maplibregl.Map({
    container: "map", style: await pickStyle(), center: HOME.center, zoom: 11.4, pitch: 0, bearing: 0,
    maxPitch: 78, antialias: true, attributionControl: false,
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");

  const NOD = { depthWriteEnabled: false, depthCompare: "always" }; // ground overlays / symbols never hidden
  function layers() {
    const real = S.mode === "real";
    const L = [
      ...aerial.map((t, i) => new deck.BitmapLayer({
        id: `aerial-${i}`, image: t.url, bounds: t.bounds, visible: S.showAer, parameters: NOD,
        opacity: real ? 0.95 : 0.75, desaturate: real ? 0.1 : 0.5, tintColor: real ? [235, 238, 245] : [140, 148, 165],
      })),
      new deck.SolidPolygonLayer({
        id: "bld", data: blds, visible: S.showB, extruded: true, pickable: true,
        getPolygon: (d) => d.ring, getElevation: (d) => d.h, getFillColor: (d) => (real ? d.nat : cellColor(d.cell)),
        material: { ambient: 0.48, diffuse: 0.62, shininess: 14, specularColor: [36, 36, 40] },
        updateTriggers: { getFillColor: [S.ver, S.mode] },
      }),
      // future city: designated building land (dashed outline) and the planned buildings on it
      new deck.SolidPolygonLayer({
        id: "fut-land", data: futAreas, visible: S.scen === "future", getPolygon: (d) => d.path,
        getFillColor: [245, 165, 36, 34], parameters: NOD,
      }),
      new deck.PathLayer({
        id: "fut-edge", data: futAreas, visible: S.scen === "future", getPath: (d) => d.path, getColor: [245, 165, 36, 230],
        widthUnits: "pixels", getWidth: 1.8, getDashArray: [5, 4], dashJustified: true,
        extensions: [new deck.PathStyleExtension({ dash: true })], parameters: NOD,
      }),
      new deck.PolygonLayer({
        id: "fut-bld", data: fblds, visible: S.scen === "future" && S.showB, extruded: true, wireframe: true, pickable: true,
        getPolygon: (d) => d.ring, getElevation: (d) => d.h * S.grow,
        getFillColor: (d) => (real ? [214, 170, 110] : cellColor(d.cell < nC ? d.cell : -1)).concat(235),
        getLineColor: [255, 196, 110, 255], lineWidthUnits: "pixels", getLineWidth: 1,
        material: { ambient: 0.5, diffuse: 0.6, shininess: 10, specularColor: [30, 30, 30] },
        updateTriggers: { getFillColor: [S.ver, S.mode, S.scen], getElevation: S.grow },
      }),
      // intro: landmark buildings in their own colour (S.lm = 1 -> 0 fade), drawn just above the city
      ...(S.lm > 0 ? LM.flatMap((l, k) => [
        new deck.ScatterplotLayer({
          id: `lm-glow-${k}`, data: [l], getPosition: (d) => [d.lon, d.lat], radiusUnits: "meters",
          getRadius: 230 + 80 * Math.sin(S.lmT * 2.4) ** 2, stroked: true, filled: true, lineWidthUnits: "pixels", getLineWidth: 2,
          getFillColor: [...l.rgb, Math.round(70 * S.lm)], getLineColor: [0, 159, 213, Math.round(230 * S.lm)], parameters: NOD,
          updateTriggers: { getRadius: S.lmT, getFillColor: S.lm, getLineColor: S.lm },
        }),
        new deck.PolygonLayer({
          id: `lm-bld-${k}`, data: l.blds, extruded: true, wireframe: true, getPolygon: (d) => d.ring, getElevation: (d) => d.h + 0.6,
          getFillColor: [...l.rgb, Math.round(255 * S.lm)], getLineColor: [0, 159, 213, Math.round(255 * S.lm)], lineWidthUnits: "pixels", getLineWidth: 1,
          material: { ambient: 0.75, diffuse: 0.5, shininess: 20, specularColor: [60, 80, 120] },
          updateTriggers: { getFillColor: S.lm, getLineColor: S.lm },
        }),
        new deck.TextLayer({
          id: `lm-label-${k}`, data: [l], getPosition: (d) => [d.lon, d.lat, 40], getText: (d) => d.sub ? `${d.name}
${d.sub}` : d.name,
          getSize: 13, getColor: [255, 255, 255, Math.round(255 * S.lm)], getPixelOffset: [0, 52], fontFamily: "Geist, Arial, sans-serif", // label below: gateway labels sit beside the campus
          fontWeight: 600, lineHeight: 1.35, background: true, backgroundPadding: [10, 6], getBackgroundColor: [...l.rgb, Math.round(240 * S.lm)],
          getBorderColor: [0, 159, 213, Math.round(255 * S.lm)], getBorderWidth: 1.5, characterSet: "auto", parameters: NOD,
          updateTriggers: { getColor: S.lm, getBackgroundColor: S.lm, getBorderColor: S.lm },
        }),
      ]) : []),
      new deck.ScatterplotLayer({
        id: "cells", data: S.showB ? flatCells : cellPts, visible: !real, getPosition: (d) => d.pos, pickable: true,
        getRadius: 18, radiusUnits: "meters", getFillColor: (d) => cellColor(d.c).concat(200),
        updateTriggers: { getFillColor: [S.ver, S.mode] },
      }),
      // model reach of the new sites: every reached 50 m cell glows softly on the ground (stronger = more
      // certain) – depth-tested, so buildings keep their own colour on top + dashed outline
      new deck.ScatterplotLayer({
        id: "newcov", data: V.newCells, visible: S.showCov, getPosition: (d) => d.pos, radiusUnits: "meters", getRadius: 26,
        getFillColor: (d) => [...ACC, Math.round((real ? 40 : 22) + 70 * (d.p - COV_P) / (1 - COV_P))],
        updateTriggers: { getFillColor: S.mode },
      }),
      new deck.PathLayer({
        id: "reach", data: V.reach, visible: S.showCov, getPath: (d) => d.path, getColor: [...ACC, 190],
        widthUnits: "pixels", getWidth: 1.6, getDashArray: [6, 5], dashJustified: true,
        extensions: [new deck.PathStyleExtension({ dash: true })], parameters: NOD,
      }),
      // provider rule: solid 300 m ring + animated radio pulse
      new deck.ScatterplotLayer({
        id: "rule", data: V.ruleRings, visible: S.showCov, getPosition: (d) => d.pos, radiusUnits: "meters", getRadius: DEV_R,
        stroked: true, filled: true, getFillColor: [...ACC, 40], getLineColor: [150, 190, 255, 255], lineWidthUnits: "pixels", getLineWidth: 2.5,
        parameters: NOD,
      }),
      new deck.ScatterplotLayer({
        id: "pulse", data: V.pulse, visible: S.showCov, getPosition: (d) => d.pos, radiusUnits: "meters",
        getRadius: (d) => DEV_R * ((S.t + d.ph) % 1), filled: false, stroked: true, lineWidthUnits: "pixels", getLineWidth: 1.5,
        getLineColor: (d) => [...ACC, Math.round(220 * (1 - ((S.t + d.ph) % 1)))], parameters: NOD,
        updateTriggers: { getRadius: S.t, getLineColor: S.t },
      }),
      new deck.ScatterplotLayer({
        id: "meters", data: meters, visible: S.showMet, getPosition: (d) => [d.lon, d.lat], pickable: true,
        radiusUnits: "meters", getRadius: 13, stroked: true, lineWidthMinPixels: 1,
        getLineColor: (d, { index }) => (V.newCovered.has(index) ? [...ACC, 255] : [8, 9, 11, 255]),
        getLineWidth: (d, { index }) => (V.newCovered.has(index) ? 6 : 1),
        getFillColor: (d) => d.rssi == null ? (d.st === "A" ? CORAL : [100, 106, 116]) : RAMPS.rssi.f(d.rssi),
        radiusMinPixels: 3, parameters: NOD, updateTriggers: { getLineColor: [S.ver], getLineWidth: [S.ver] },
      }),
      // existing gateways: halo ring on the ground (constant size on screen, visible at every zoom level)
      new deck.ScatterplotLayer({
        id: "exist-halo", data: V.existing, getPosition: (d) => d.pos, radiusUnits: "pixels", getRadius: 8,
        filled: true, stroked: true, lineWidthUnits: "pixels", getLineWidth: 1.5,
        getFillColor: (d) => (d.on ? [255, 255, 255, 30] : [255, 107, 107, 40]),
        getLineColor: (d) => (d.on ? [255, 255, 255, 200] : [255, 107, 107, 220]),
        parameters: NOD, updateTriggers: { getFillColor: [S.ver], getLineColor: [S.ver] },
      }),
      // gateways: mast from the ground to the antenna, icon at the top
      new deck.LineLayer({
        id: "masts", data: V.glyphs, getSourcePosition: (d) => d.pos, getTargetPosition: (d) => d.top,
        getColor: (d) => d.mast, getWidth: (d) => d.w, widthUnits: "pixels",
      }),
      new deck.ScatterplotLayer({
        id: "mast-foot", data: V.glyphs.filter((g) => g.size > 20), getPosition: (d) => d.pos, radiusUnits: "pixels",
        getRadius: 3, getFillColor: (d) => d.mast, parameters: NOD,
      }),
      new deck.IconLayer({
        id: "site-icons", data: V.glyphs, pickable: true, getPosition: (d) => d.top, sizeUnits: "pixels",
        iconAtlas: ATLAS.atlas, iconMapping: ATLAS.mapping, getIcon: (d) => d.icon, getSize: (d) => d.size,
        parameters: NOD, onClick: ({ object: g }) => { if (g) toggleSite(g.s); return true; },
      }),
      new deck.TextLayer({
        id: "exist-labels", data: V.existing, visible: !S.cleanShot, // cleanShot: label-free stills for the pitch page getPosition: (d) => d.top, getText: (d) => d.text, getSize: 11.5,
        getColor: (d) => (d.on ? [20, 22, 26] : [255, 255, 255]), getPixelOffset: (d) => d.off,
        getTextAnchor: (d) => d.anchor, getAlignmentBaseline: "center",
        fontFamily: "Geist, Arial, sans-serif", fontWeight: 600, background: true, backgroundPadding: [6, 3],
        getBackgroundColor: (d) => (d.on ? [240, 242, 245, 235] : [200, 60, 64, 235]), parameters: NOD,
        characterSet: "auto", updateTriggers: { getColor: [S.ver], getBackgroundColor: [S.ver] },
      }),
      new deck.TextLayer({
        id: "labels", data: V.labels, visible: !S.cleanShot, getPosition: (d) => d.pos, getText: (d) => d.text, getSize: 12,
        getColor: [236, 238, 241], getPixelOffset: [0, -30], fontFamily: "Geist, Arial, sans-serif", fontWeight: 600,
        background: true, getBackgroundColor: [12, 14, 18, 235], backgroundPadding: [6, 3], parameters: NOD,
      }),
    ];
    if (S.hoverCell >= 0) {
      const c = S.hoverCell, links = [];
      for (const s of V.act) {
        const p = P[c * nS + s];
        if (p > 0.15) links.push({ p, from: [CL.lon[c], CL.lat[c], 8], to: [sites[s].lon, sites[s].lat, topZ(sites[s])] });
      }
      L.push(new deck.LineLayer({
        id: "links", data: links, getSourcePosition: (d) => d.from, getTargetPosition: (d) => d.to,
        getWidth: (d) => 1 + d.p * 4, getColor: (d) => RAMPS.cov.f(d.p).concat(230),
      }));
    }
    return L;
  }

  // soft daylight: sky ambient + low afternoon sun from the south-west
  const lighting = new deck.LightingEffect({
    ambient: new deck.AmbientLight({ color: [255, 255, 255], intensity: 1.1 }),
    sun: new deck.DirectionalLight({ color: [255, 244, 228], intensity: 1.45, direction: [1.5, 2.5, -2.2] }),
    fill: new deck.DirectionalLight({ color: [180, 200, 255], intensity: 0.35, direction: [-1.5, -1, -1] }),
  });
  buildView();
  const overlay = new deck.MapboxOverlay({ interleaved: false, layers: layers(), effects: [lighting], onHover,
    getCursor: ({ isHovering }) => (isHovering ? "pointer" : "grab") });
  map.addControl(overlay);
  const redraw = () => overlay.setProps({ layers: layers() });

  // radio pulse animation around new sites (~30 fps, only while there is something to animate)
  let raf = 0, last = 0;
  const T0 = performance.now();
  function frame(now) {
    if (!V.pulse.length || !S.showCov || document.hidden) { raf = 0; return; }
    if (now - last > 33) { S.t = ((now - T0) % 2800) / 2800; last = now; redraw(); }
    raf = requestAnimationFrame(frame);
  }
  const animate = () => { if (!raf && V.pulse.length && S.showCov) raf = requestAnimationFrame(frame); };
  document.addEventListener("visibilitychange", animate);

  // ---------- panel ----------
  const delta = (id, d, fmt, goodUp = true) => {
    const el = $(id);
    if (Math.abs(d) < 1e-6) { el.textContent = ""; el.className = "delta"; return; }
    el.textContent = (d > 0 ? "+" : "−") + fmt(Math.abs(d));
    el.className = "delta " + ((d > 0) === goodUp ? "up" : "down");
  };
  function refresh() {
    R = compute(active());
    S.cov = newSiteCoverage();
    S.ver++;
    const s = R.summary;
    $("s-cov").textContent = pct(s.cov);
    $("s-red").textContent = pct(s.red);
    $("s-gap").textContent = fmtN(s.gaps);
    $("m-base").style.width = (100 * Math.min(BASE.cov, s.cov)).toFixed(2) + "%";
    $("m-now").style.width = (100 * s.cov).toFixed(2) + "%";
    delta("d-cov", s.cov - BASE.cov, (x) => (100 * x).toFixed(1) + " pt");
    delta("d-red", s.red - BASE.red, (x) => (100 * x).toFixed(1) + " pt");
    delta("d-gap", s.gaps - BASE.gaps, fmtN, false);
    const rad = withinRadius(active());
    $("s-rad").textContent = pct(rad);
    delta("d-rad", rad - BASE_RAD, (x) => (100 * x).toFixed(1) + " pt");
    const dv = s.eur - BASE.eur;
    $("value-line").innerHTML = `≈ <b>${fmtN(s.readable)}</b> meters readable by radio · manual-visit value ≈ <b>${eur(s.eur)}</b> per reading round` +
      (Math.abs(dv) > 1 ? ` <span class="${dv > 0 ? "up" : "down"}">(${dv > 0 ? "+" : "−"}${eur(Math.abs(dv))})</span>` : "");
    renderPlan();
    renderFutureCheck();
    renderLegend();
    if (S.pm === "target") setTarget(S.target); // curve + maximum change when an existing gateway is switched off
    buildView();
    redraw();
    animate();
  }

  function toggleSite(s) {
    if (s.kind === "existing") S.disabled.has(s.i) ? S.disabled.delete(s.i) : S.disabled.add(s.i);
    else S.extra.has(s.i) ? S.extra.delete(s.i) : S.extra.add(s.i);
    refresh(); writeHash();
  }

  function marginal(sIdx) {
    let g = 0;
    for (let c = 0; c < nC; c++) g += W[c] * R.q0[c] * Math.min(P[c * nS + sIdx], 0.999);
    return g;
  }

  const CONF = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" };
  function siteCard(i, rankHtml) {
    const s = sites[i], x = covBySite().get(i);
    return `<li class="site" data-s="${i}">
      <div class="site-head">${rankHtml}<span class="sid">${s.id}</span><span class="tag ${s.conf}">${CONF[s.conf] || s.conf}</span>
        <button class="site-x" data-x="${i}" title="Remove this site" aria-label="Remove ${s.id}">×</button></div>
      ${x ? `<div class="site-stats">
        <div class="stat-cell"><b>${fmtN(x.addr300)}</b>addresses within ${DEV_R} m</div>
        <div class="stat-cell"><b>${x.met300}</b>measured meters within ${DEV_R} m</div>
        <div class="stat-cell"><b>${fmtN(x.addrNew)}</b>newly covered (model)</div>
        <div class="stat-cell"><b>≈ ${x.radius >= 1000 ? (x.radius / 1000).toFixed(1) + " km" : fmtN(x.radius) + " m"}</b>model reach · ${s.h.toFixed(0)} m antenna</div>
      </div>` : ""}</li>`;
  }
  function renderPlan() {
    const plannedIds = S.plan.filter((i) => S.extra.has(i));
    const manualIds = [...S.extra].filter((i) => !S.plan.includes(i));
    const off = [...S.disabled].map((i) => `<li class="site" data-s="${i}"><div class="site-head"><span class="rank off">!</span>
      <span class="sid">${sites[i].name}</span><span class="tag low">Offline</span>
      <button class="site-x" data-x="${i}" title="Switch back on" aria-label="Switch ${sites[i].name} back on">↺</button></div></li>`);
    let html;
    if (plannedIds.length + manualIds.length > 5) {
      // compact build order: gain of each site on top of the ones before it, cumulative coverage
      const q0 = missOf(baseSites());
      let prev = covFromMiss(q0);
      const rows = plannedIds.concat(manualIds).map((i, k) => {
        for (let c = 0; c < nC; c++) q0[c] *= 1 - P[c * nS + i];
        const cv = covFromMiss(q0), gain = cv - prev; prev = cv;
        const manual = !S.plan.includes(i), s = sites[i];
        return `<li class="row-site" data-s="${i}" title="${CONF[s.conf] || s.conf} · ≈ ${s.h.toFixed(0)} m antenna">
          <span class="rank${manual ? " manual" : ""}">${manual ? "+" : k + 1}</span>
          <span><b>${s.id}</b><span class="dot ${s.conf}"></span></span>
          <span class="gain">+${(100 * gain).toFixed(1)}</span><span class="cum">${pct(cv)}</span>
          <button class="site-x" data-x="${i}" title="Remove this site" aria-label="Remove ${s.id}">×</button></li>`;
      });
      html = `<li class="rows-head"><span>#</span><span>Site · confidence</span><span>+ pt</span><span>total</span><span></span></li>${rows.join("")}`;
      $("plan-list").className = "rows";
    } else {
      html = plannedIds.map((i, k) => siteCard(i, `<span class="rank">${k + 1}</span>`))
        .concat(manualIds.map((i) => siteCard(i, `<span class="rank manual">+</span>`))).join("");
      $("plan-list").className = "results";
    }
    $("plan-list").innerHTML = html + off.join("");
    $("plan-empty").hidden = !!(plannedIds.length + manualIds.length + off.length);
    // summary of a target plan
    const I = S.planInfo, box = $("plan-summary");
    if (I && plannedIds.length === I.k && S.plan.length === I.k) {
      const now = R.summary.cov;
      box.innerHTML = I.reached
        ? `<b>${I.k}</b> of ${candidates.length} ${SITEWORD} reach <b>${pct(now)}</b> coverage (target ${pct(I.target)}).
           All ${candidates.length} together would give ${pct(I.max)} – <b>${candidates.length - I.k}</b> sites are not needed.`
        : `<span class="warn">The target is above what all ${candidates.length} ${SITEWORD} can reach (${pct(I.max)}).</span>
           Showing all sites in build order.`;
      box.hidden = false;
    } else box.hidden = true;
  }
  $("plan-list").addEventListener("click", (e) => {
    const x = e.target.closest("[data-x]");
    if (x) { toggleSite(sites[+x.dataset.x]); e.stopPropagation(); return; }
    const li = e.target.closest(".site, .row-site");
    if (li) flyToSite(sites[+li.dataset.s]);
  });

  function renderLegend() {
    if (S.mode === "real") {
      $("legend").innerHTML = `<div class="note2">Buildings in neutral grey – the map focuses on the gateways:
        solid ring = ${DEV_R} m device radius, dashed ring and glow = where the model predicts ≥ 50 % reception.</div>`;
      return;
    }
    const r = RAMPS[S.mode], n = 14, cols = [];
    for (let i = 0; i <= n; i++) cols.push(`rgb(${r.f(r.lo + (r.hi - r.lo) * i / n).join(",")})`);
    const lb = INS.link_budget || {};
    $("legend").innerHTML = `<div class="bar" style="background:linear-gradient(90deg,${cols.join(",")})"></div>
      <div class="ends"><span>${r.l}</span><span>${r.r}</span></div>` +
      (S.mode === "rssi" && lb.gateway_sensitivity_dbm ? `<div class="note2" style="margin-top:6px">Gateway limit ${lb.gateway_sensitivity_dbm} dBm (SF12, datasheet);
        in the city the weakest received signals are ≈ ${Math.round((INS.radio_observed || {}).rssi_floor_dbm ?? -123)} dBm because of radio noise.</div>` : "");
  }

  // ---------- future city ----------
  const USE_NAME = { residential: "residential", mixed: "mixed use", commercial: "commercial / industrial", special: "special / public use" };
  const futNote = (o) => {
    const a = areaById[o.area] || {};
    return `<div class="tt-foot">Planned building (${USE_NAME[o.use] || o.use}), worst case ${o.h.toFixed(0)} m – as tall as the tallest 10 % of
      comparable buildings nearby. ${a.kind || "Plan area"}${a.plan ? ": " + a.plan : ""}, ${a.ha} ha still unbuilt today.</div>`;
  };
  // coverage of a set of gateways in another city scenario (same meter type)
  function covIn(sc, act) {
    const B_ = SCN[sc], Pp = (B_.DEV[S.device] || B_.DEV.mix).p;
    let a = 0;
    for (let c = 0; c < B_.nC; c++) {
      let q = 1;
      for (const s of act) q *= 1 - Math.min(Pp[c * nS + s], 0.999);
      a += B_.W[c] * (1 - q);
    }
    return a / B_.WT;
  }
  function renderFutureCheck() {
    const box = $("future-check");
    if (!SCN.future || !S.extra.size) { box.hidden = true; return; }
    const act = active(), t = covIn("today", act), f = covIn("future", act);
    const tgt = S.planInfo ? S.planInfo.target : null, ok = (v) => tgt == null || v >= tgt - 1e-9;
    const fs = FUT.summary;
    box.innerHTML = `<div class="fc-head">Future-proof check</div>
      This plan reaches <b class="${ok(t) ? "ok" : "no"}">${pct(t)}</b> in today's city and
      <b class="${ok(f) ? "ok" : "no"}">${pct(f)}</b> in the future city
      (${fmtN(fs.future_buildings)} planned buildings on ${fs.open_ha.toFixed(0)} ha of designated building land, each with a new meter).
      ${S.scen === "today" && tgt != null && !ok(f) ? `<br>Switch <b>City → Future city</b> and plan again to stay above ${pct(tgt)} after the new districts are built.` : ""}
      ${S.scen === "future" && tgt != null ? `<br>Planned for the future city – it already holds today.` : ""}`;
    box.hidden = false;
  }
  function setScenario(sc) {
    S.scen = SCN[sc] ? sc : "today";
    const B_ = SCN[S.scen];
    nC = B_.nC; CL = B_.CL; DEV = B_.DEV; RS = B_.RS; W = B_.W; WT = B_.WT; SH = B_.SH; NEAR = B_.NEAR;
    cellPts = B_.cellPts; flatCells = B_.flatCells;
    setDevice(S.device);
    BASE_RAD = withinRadius(existing);
    document.querySelectorAll("#scen button").forEach((b) => {
      const on = b.dataset.scen === S.scen; b.classList.toggle("on", on); b.setAttribute("aria-checked", on);
    });
    $("mk-fut").hidden = S.scen !== "future";
    if (!SCN.future) return;
    const fs = FUT.summary;
    $("scen-hint").innerHTML = S.scen === "future"
      ? `Adds <b>${fmtN(fs.future_buildings)}</b> planned buildings (orange outline) on ${fs.open_ha.toFixed(0)} ha that the land-use plans
         designate for building but is still farmland today – as obstacles at worst-case height and as ${fmtN(fs.future_meters)} new meters.`
      : `Buildings and addresses as they are today. <b>Future city</b> adds what Heilbronn's land-use plans allow to be built.`;
  }
  if (SCN.future) {
    $("scen-box").hidden = false;
    document.querySelectorAll("#scen button").forEach((b) => (b.onclick = () => {
      setScenario(b.dataset.scen);
      if (S.planInfo) runTarget(); // a target plan is re-optimised for the chosen city
      if (S.pm === "target") setTarget(S.target);
      refresh(); writeHash();
    }));
  }

  // ---------- tooltip ----------
  const tt = (title, sub, rows, extra = "") => `<div class="tt-head"><div><div class="tt-title">${title}</div>
    ${sub ? `<div class="tt-sub">${sub}</div>` : ""}</div></div>
    ${rows.length ? `<div class="tt-grid">${rows.map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("")}</div>` : ""}${extra}`;
  function onHover(info) {
    const tip = $("tooltip");
    const o = info.object, id = info.layer && info.layer.id;
    let cell = -1, html = "";
    if (o && (id === "bld" || id === "cells" || id === "fut-bld")) {
      cell = id === "cells" ? o.c : o.cell;
      if (cell >= nC) cell = -1;
      if (cell >= 0) {
        const bs = R.bestS[cell];
        html = tt(CL.ex[cell], `${CL.n[cell]} address${CL.n[cell] === 1 ? "" : "es"} in this ${C.cell_size} m cell`, [
          ["Reception", `<b>${pct(R.cov[cell], 0)}</b>`],
          ["Backup (≥ 2 gateways)", pct(R.red[cell], 0)],
          ["Best gateway", bs >= 0 ? sites[bs].name : "none in reach"],
          ...(bs >= 0 && !Number.isNaN(R.best[cell]) ? [["Expected signal", `${R.best[cell].toFixed(0)} dBm`]] : []),
          ["Model uncertainty", `± ${(100 * R.unc[cell]).toFixed(0)} pt`],
        ], (id === "fut-bld" ? futNote(o) : "") + (SH[cell] > 0 ? `<div class="tt-warn">${Math.round(100 * SH[cell])} % of addresses here sit &gt; 40 m from the street – the meter is likely
          in a shaft (harder by radio, €${BC.visit_cost_shaft} per manual visit).</div>` : ""));
      } else html = tt("Building", `${o.h.toFixed(1)} m high · no registered address`, []);
    } else if (o && id === "site-icons") {
      const s = o.s, on = s.kind === "existing" ? !S.disabled.has(s.i) : S.extra.has(s.i);
      const g = on ? null : marginal(s.i), x = covBySite().get(s.i);
      const kind = s.kind === "existing" ? (on ? "Existing gateway" : "Existing gateway · switched off") : on ? "New gateway" : "Candidate site";
      html = tt(s.kind === "existing" ? s.name : s.id, kind, [
        ["Antenna height", `≈ ${s.h.toFixed(0)} m above ground`],
        ...(s.mount ? [["Mounting (assumed)", { roof: "building roof + 2 m", structure: "tower / structure + 2 m",
          "free mast": "free-standing 10 m mast" }[s.mount] || s.mount]] : []),
        ["Ground", s.ground == null ? "n/a" : `${s.ground.toFixed(0)} m a.s.l.`],
        ...(s.kind === "existing" ? [["Meters heard (measured week)", s.devices]] : []),
        ...(g != null ? [["Would add", `<b>+${fmtN(g)}</b> covered addresses`]] : []),
        ...(x ? [[`Within ${DEV_R} m`, `${fmtN(x.addr300)} addresses · ${x.met300} meters`],
          ["Model reach (≥ 50 %)", `≈ ${fmtN(x.radius)} m · ${fmtN(x.addr)} addresses`],
          ["Not covered before", `${fmtN(x.addrNew)} addresses · ${x.metNew} meters`]] : []),
        ["Prediction confidence", s.conf],
      ], `<div class="tt-foot">Click to switch ${on ? "off" : "on"}</div>`);
    } else if (o && id === "meters") {
      html = tt("Water meter", o.adr || "", o.rssi == null
        ? [["Status", o.st], ["Received this week", "<span style='color:var(--bad)'>no</span>"]]
        : [["Status", o.st], ["Best signal", `${o.rssi} dBm`], ["Via", o.gw], ["Gateways", o.n_gw],
          ["Delivery ratio", o.pdr == null ? "n/a" : Math.round(100 * o.pdr) + " %"]],
        o.src && o.src !== "provided" ? `<div class="tt-foot">Location repaired from the street address</div>` : "");
    }
    if (html) {
      tip.innerHTML = html; tip.hidden = false;
      const w = tip.offsetWidth, h = tip.offsetHeight;
      tip.style.left = Math.min(info.x + 16, window.innerWidth - w - 12) + "px";
      tip.style.top = Math.min(info.y + 16, window.innerHeight - h - 12) + "px";
    } else tip.hidden = true;
    if (cell !== S.hoverCell) { S.hoverCell = cell; redraw(); }
  }

  // ---------- camera ----------
  const pad = () => ({ top: 70, bottom: 70, left: window.innerWidth > 900 ? 390 : 30, right: window.innerWidth > 900 ? 190 : 30 });
  function flyToSite(s) {
    map.flyTo({ center: [s.lon, s.lat], zoom: 14.3, pitch: 60, bearing: map.getBearing(), duration: 1400, padding: pad(), essential: true });
  }
  function focusNewSites() {
    if (!S.cov.length) return;
    let w = 180, e = -180, so = 90, n = -90;
    for (const x of S.cov) {
      const st = sites[x.s], r = Math.max(x.radius, DEV_R);
      w = Math.min(w, st.lon - r / KX); e = Math.max(e, st.lon + r / KX); so = Math.min(so, st.lat - r / KY); n = Math.max(n, st.lat + r / KY);
    }
    map.fitBounds([[w, so], [e, n]], { padding: pad(), pitch: 56, bearing: map.getBearing(), duration: 1600, maxZoom: 15 });
  }

  // ---------- controls ----------
  const GOALS = {
    radius: `Picks the sites that bring the most addresses within ${DEV_R} m of a gateway – the provider's rule.`,
    coverage: "Picks the sites with the highest expected coverage predicted by the reception model.",
    robust: "Like Max coverage, but only counts predictions the model is confident about (probability − 1σ).",
    redundancy: "Balances new coverage with a second gateway as backup for addresses that are already covered.",
  };
  function setGoal(g) {
    S.goal = GOALS[g] ? g : "coverage";
    document.querySelectorAll("#goal button").forEach((b) => {
      const on = b.dataset.goal === S.goal; b.classList.toggle("on", on); b.setAttribute("aria-checked", on);
    });
    $("goal-hint").textContent = GOALS[S.goal];
  }
  document.querySelectorAll("#goal button").forEach((b) => (b.onclick = () => setGoal(b.dataset.goal)));
  const setK = (k) => { S.k = Math.max(1, Math.min(10, k)); $("k-out").textContent = S.k; };
  $("k-minus").onclick = () => setK(S.k - 1);
  $("k-plus").onclick = () => setK(S.k + 1);

  // ---------- meter type: today's manufacturer mix, or plan as if every meter were the weakest type ----------
  const MAKER_NAMES = { APA: "Apator", IMT: "IMT", AXI: "Axioma" };
  const mixText = () => Object.entries(DEVINFO.mix || {}).sort((a, b) => b[1] - a[1])
    .map(([m, w]) => `${MAKER_NAMES[m] || m} ${Math.round(100 * w)} %`).join(" · ");
  function setDevice(d) {
    S.device = DEV[d] ? d : "mix";
    P = DEV[S.device].p; SD = DEV[S.device].sd;
    PLO = P.map((p, i) => Math.max(0, p - SD[i]));
    BASE_R = compute(existing); BASE = BASE_R.summary;
    curveCache = null;
    document.querySelectorAll("#device button").forEach((b) => {
      const on = b.dataset.dev === S.device; b.classList.toggle("on", on); b.setAttribute("aria-checked", on);
    });
    const weak = MAKER_NAMES[DEVINFO.weak] || DEVINFO.weak || "the weakest type";
    $("dev-weak").textContent = DEV.weak ? `Weakest (${weak})` : "Weakest type";
    $("device-hint").textContent = S.device === "weak"
      ? `Conservative: every meter behaves like ${weak}, the manufacturer with the lowest reception in the measured week.`
      : `Meters as installed today: ${mixText() || "measured meters"}.`;
  }
  document.querySelectorAll("#device button").forEach((b) => (b.onclick = () => {
    setDevice(b.dataset.dev);
    if (S.planInfo) runTarget(); // a target plan is re-optimised for the chosen meter type
    if (S.pm === "target") setTarget(S.target);
    refresh(); writeHash();
  }));
  if (!DEV.weak) { $("dev-weak").disabled = true; $("dev-weak").title = "Re-run uv run hnplan all to compute the weakest-type scenario"; }

  // ---------- planning mode: coverage target (default) or fixed number of sites ----------
  const btnLabel = () => (S.pm === "target" ? "Find minimum sites" : "Find best sites");
  function setPlanMode(pm) {
    S.pm = pm === "fixed" ? "fixed" : "target";
    document.querySelectorAll("#plan-mode button").forEach((b) => {
      const on = b.dataset.pm === S.pm; b.classList.toggle("on", on); b.setAttribute("aria-selected", on);
    });
    $("pm-target").hidden = S.pm !== "target";
    $("pm-fixed").hidden = S.pm !== "fixed";
    $("btn-opt").textContent = btnLabel();
    if (S.pm === "target") drawCurve();
  }
  document.querySelectorAll("#plan-mode button").forEach((b) => (b.onclick = () => setPlanMode(b.dataset.pm)));

  const maxCov = () => { const G = coverageCurve(); return G.curve.length ? G.curve[G.curve.length - 1] : G.cov0; };
  function setTarget(t) {
    const maxPct = Math.floor(maxCov() * 1000) / 10; // e.g. 99.3 – highest target the approved sites can reach
    const el = $("target");
    el.max = maxPct;
    S.target = Math.min(Math.max(t, 0.6), maxPct / 100);
    el.value = (100 * S.target).toFixed(1);
    $("t-out").textContent = pct(S.target);
    $("t-max").textContent = `max ${maxPct.toFixed(1)} % (all ${candidates.length} sites)`;
    drawCurve();
  }
  $("target").oninput = (e) => setTarget(+e.target.value / 100);

  // coverage vs number of new gateways (greedy build order), with the target line
  function drawCurve() {
    const G = coverageCurve(), n = G.curve.length, pts = [G.cov0, ...G.curve];
    const x0 = 30, x1 = 314, y0 = 10, y1 = 112;
    const lo = Math.max(0, Math.floor(Math.min(G.cov0, S.target) * 10) / 10);
    const X = (k) => x0 + ((x1 - x0) * k) / Math.max(n, 1), Y = (v) => y1 - ((y1 - y0) * (v - lo)) / (1 - lo);
    const line = pts.map((v, k) => `${X(k).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
    const k = G.curve.findIndex((c) => c >= S.target) + 1;
    const yTicks = [lo, (lo + 1) / 2, 1].map((v) => `<line class="grid" x1="${x0}" x2="${x1}" y1="${Y(v)}" y2="${Y(v)}"/>
      <text x="${x0 - 4}" y="${Y(v) + 3}" text-anchor="end">${Math.round(100 * v)}%</text>`).join("");
    const tstep = n > 140 ? 50 : n > 70 ? 20 : 10;
    const xTicks = Array.from({ length: Math.floor(n / tstep) + 1 }, (_, i) => i * tstep).map((t) =>
      `<text x="${X(t)}" y="${y1 + 13}" text-anchor="middle">${t}</text>`).join("");
    const ty = Y(S.target);
    const mark = k > 0 ? `<line class="kline" x1="${X(k)}" x2="${X(k)}" y1="${y1}" y2="${Y(G.curve[k - 1])}"/>
      <circle class="dot" cx="${X(k)}" cy="${Y(G.curve[k - 1])}" r="4"/>
      <text class="lbl" x="${Math.min(X(k) + 7, x1 - 44)}" y="${Math.min(Y(G.curve[k - 1]) + 16, y1 - 4)}">≈ ${k} sites</text>` : "";
    $("curve").innerHTML = `${yTicks}${xTicks}
      <text x="${x1}" y="${y1 + 24}" text-anchor="end">new gateways</text>
      <polygon class="area" points="${X(0)},${y1} ${line} ${X(n)},${y1}"/>
      <polyline class="line" points="${line}"/>
      <line class="tline" x1="${x0}" x2="${x1}" y1="${ty}" y2="${ty}"/>
      <text class="tlbl" x="${x0 + 4}" y="${ty - 4}">target ${pct(S.target)}</text>${mark}`;
    $("target-hint").innerHTML = k > 0
      ? `About <b>${k}</b> of ${n} ${SITEWORD} are needed (greedy estimate). <b>Find minimum sites</b> also checks
         whether a smarter combination needs fewer.`
      : `Even all ${n} ${SITEWORD} reach only ${pct(maxCov())}.`;
  }

  function writeHash() {
    const h = new URLSearchParams();
    if (S.plan.length && S.planInfo) h.set("t", (100 * S.planInfo.target).toFixed(1));
    else if (S.plan.length) h.set("k", S.plan.length), h.set("goal", S.goal);
    const man = [...S.extra].filter((i) => !S.plan.includes(i)).map((i) => sites[i].id);
    if (man.length) h.set("add", man.join(","));
    if (S.disabled.size) h.set("off", [...S.disabled].map((i) => sites[i].id).join(","));
    if (S.mode !== "cov") h.set("mode", S.mode);
    if (S.device !== "mix") h.set("dev", S.device);
    if (S.scen !== "today") h.set("city", S.scen);
    if (!S.showExist) h.set("ex", "0");
    if (S.showMet) h.set("met", "1");
    if (S.showAer) h.set("aer", "1");
    try { history.replaceState(null, "", h.toString() ? "#" + h : location.pathname); } catch (e) { /* sandboxed */ }
  }
  function runTarget() { // fewest approved sites reaching the target
    const r = minSites(S.target);
    S.plan = r.order; S.extra = new Set(r.order);
    S.planInfo = { target: S.target, k: r.k, max: r.max, reached: r.reached, swaps: r.swaps, greedyK: r.greedyK };
  }
  function runFixed() { // best k sites for the chosen goal
    const chosen = greedy(S.k, S.goal);
    S.plan = chosen; S.extra = new Set(chosen); S.planInfo = null;
  }
  $("btn-opt").onclick = () => {
    const b = $("btn-opt");
    b.disabled = true; b.textContent = "Calculating…";
    setTimeout(() => {
      (S.pm === "target" ? runTarget : runFixed)();
      refresh(); writeHash(); focusNewSites();
      b.disabled = false; b.textContent = btnLabel();
    }, 30);
  };
  $("btn-reset").onclick = () => { S.plan = []; S.planInfo = null; S.extra.clear(); S.disabled.clear(); refresh(); writeHash();
    map.easeTo({ ...HOME, padding: { left: 0, right: 0, top: 0, bottom: 0 }, duration: 1200 }); };

  function setMode(m) {
    S.mode = RAMPS[m] || m === "real" ? m : "cov";
    document.querySelectorAll("#mode button").forEach((b) => {
      const on = b.dataset.mode === S.mode; b.classList.toggle("on", on); b.setAttribute("aria-checked", on);
    });
  }
  document.querySelectorAll("#mode button").forEach((b) => (b.onclick = () => { setMode(b.dataset.mode); refresh(); writeHash(); }));

  const switches = [...document.querySelectorAll(".switch input")];
  const syncSwitches = () => switches.forEach((i) => { i.checked = !!S[i.dataset.t]; });
  switches.forEach((i) => (i.onchange = () => { S[i.dataset.t] = i.checked; buildView(); redraw(); animate(); writeHash(); }));
  if (!aerial.length) {
    const a = document.querySelector('[data-t="showAer"]');
    a.disabled = true; $("sw-aer").classList.add("disabled");
    $("sw-aer").title = "Needs DOP20 RGBI tiles in data/raw, then: uv run hnplan all --force";
  }
  $("l-rad").textContent = `Within ${DEV_R} m`;
  $("mk-rad").textContent = `${DEV_R} m device radius`;

  // shareable scenarios, e.g.  #k=3&goal=radius&off=00007076FF030429&mode=red&why=1
  function readHash() {
    const h = new URLSearchParams(location.hash.slice(1));
    const byId = Object.fromEntries(sites.map((s) => [s.id, s.i]));
    (h.get("off") || "").split(",").filter((x) => x in byId).forEach((x) => S.disabled.add(byId[x]));
    (h.get("add") || "").split(",").filter((x) => x in byId).forEach((x) => S.extra.add(byId[x]));
    setGoal(h.get("goal") || "coverage");
    setDevice(h.get("dev") || "mix");
    if (h.get("city") === "future" && SCN.future) setScenario("future");
    if (h.get("t")) { setPlanMode("target"); setTarget(+h.get("t") / 100); runTarget(); }
    else if (h.get("k")) { setPlanMode("fixed"); setK(+h.get("k") || 3); runFixed(); }
    setMode(h.get("mode") || "cov");
    if (h.get("met") === "1") S.showMet = true;
    if (h.get("ex") === "0") S.showExist = false;
    if (h.get("aer") === "1" && aerial.length) S.showAer = true;
    syncSwitches();
    if (h.get("why") === "1") $("why").hidden = false;
  }
  $("btn-why").onclick = () => { $("why").hidden = false; };
  $("why-close").onclick = () => { $("why").hidden = true; };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("why").hidden = true; });

  // ---------- methodology drawer ----------
  function renderWhyCity() {
    const c = CITY, m = c.model || {}, mt = c.mounts || {}, p0 = (x) => pct(x, 0);
    const ms = (INS.plan && INS.plan.min_sites) || [];
    $("why-body").innerHTML = `
      <h2>How the planner works for ${CITYNAME}</h2>
      <div class="kpis-why">
        <div class="kpi-why"><b>${fmtN(c.addresses)}</b><span>official house coordinates = water meters, in ${fmtN(c.cells)} cells of ${c.cell_m || 50} m</span></div>
        <div class="kpi-why"><b>${fmtN(c.buildings)}</b><span>3D buildings (LGL LoD2) checked on every radio path</span></div>
        <div class="kpi-why"><b>${c.candidates}</b><span>rooftop sites proposed automatically – no candidate list needed</span></div>
      </div>
      <h4>1 · Where could a gateway go?</h4>
      <p class="note">HNVG mounts gateways on a top-level surface. Without a list of approved sites the planner proposes its own: the area is cut into
      ${c.spacing_m} m squares, and in every square with addresses the <b>highest roof</b> (ground + building height) becomes a site, antenna 2 m above
      the roof. Mounting: ${Object.entries(mt).map(([k, v]) => `${v} ${k}`).join(", ")}; median antenna ${c.antenna_median_m.toFixed(0)} m above ground.
      A city's real list (or existing gateways in <code>raw/gateways.csv</code>) can replace or extend it.</p>
      <h4>2 · Will a meter be heard? – a model trained on real measurements</h4>
      <p class="note">There are no measurements for ${CITYNAME}. The reception model is the one trained on HNVG's measured week in Heilbronn
      (${fmtN(m.n_links || 0)} meter–gateway links, ${m.n_devices || "–"} meters). It only uses things every city has:
      distance, antenna height, height difference, buildings / terrain / forest on the path, building density at the meter and the meter maker.
      <b>Absolute ground elevation is left out</b> – Heilbronn lies at 157–218 m, ${CITYNAME} much higher, and that number means nothing across cities.</p>
      <table><tr><th>Validation on Heilbronn</th><th>New gateway (leave-one-out) AUC</th><th>Log-loss</th></tr>
        <tr><td>Heilbronn model (all features)</td><td>${m.heilbronn_logo_auc ? m.heilbronn_logo_auc.toFixed(3) : "–"}</td><td>${m.heilbronn_logo_logloss ? m.heilbronn_logo_logloss.toFixed(3) : "–"}</td></tr>
        <tr class="sel"><td>Transfer model used here (without elevation)</td><td>${m.logo_auc ? m.logo_auc.toFixed(3) : "–"}</td><td>${m.logo_logloss ? m.logo_logloss.toFixed(3) : "–"}</td></tr></table>
      <h4>3 · Fewest sites per coverage target</h4>
      <table><tr><th>Target</th><th>Rooftop sites needed</th></tr>
        ${ms.map((r) => `<tr><td>${p0(r.target)}</td><td>${r.reached ? r.k : "not reachable"}</td></tr>`).join("")}</table>
      <p class="note">Same algorithm as for Heilbronn: greedy build order, shortest prefix reaching the target, then sites the others make redundant are removed
      (the 2-for-1 swap step is skipped with more than ${SWAP_MAX} candidates).</p>
      <h4>Limits – be honest</h4>
      <p class="note">${p0(c.extrap_share)} of the radio paths lie partly outside what the Heilbronn week contained (taller roofs, deeper valleys, taller
      buildings at the meter); the model then holds the nearest known value, it does not invent better reception – more obstruction can never raise
      the probability. Every address is assumed to have one basement meter. Terrain: ${c.terrain_source}.
      <b>Next step for a real rollout:</b> one week of measurements from 2–3 test gateways in ${CITYNAME} to recalibrate the model – the pipeline is ready for it.</p>`;
  }
  function renderWhy() {
    if (CITY) return renderWhyCity();
    const I = INS, M = I.model, p0 = (x) => pct(x, 0);
    const st = Object.fromEntries(I.meter_status.map((r) => [r.meter_state, r]));
    const cv = (s, m) => M.cv[`${s}/${m}`];
    const sc = I.rssi_scatter;
    const X = (d) => 40 + (Math.log10(d) - 1) / (Math.log10(8000) - 1) * 470, Y = (r) => 10 + (-70 - r) / 60 * 170;
    const dots = sc.map((p) => `<circle cx="${X(Math.max(p.d_m, 10)).toFixed(1)}" cy="${Y(p.rssi_med).toFixed(1)}" r="2.1" fill="#4c8dff" opacity=".5"/>`).join("");
    const med = I.rssi_by_distance.map((b) => `${X(Math.sqrt(Math.max(b.d_from, 10) * b.d_to)).toFixed(1)},${Y(b.rssi_med).toFixed(1)}`).join(" ");
    const xt = [10, 100, 1000].map((d) => `<text x="${X(d)}" y="196">${d >= 1000 ? d / 1000 + " km" : d + " m"}</text>`).join("");
    const yt = [-80, -100, -120].map((r) => `<text x="4" y="${Y(r) + 3}">${r}</text>`).join("");
    const imp = M.importance.slice(0, 8), mx = Math.max(...imp.map((d) => d[1]), 1e-6);
    const label = { log_d: "distance", d_m: "distance", dz_m: "height difference", terr_obs_max_m: "terrain in the way",
      rx_ground_z: "ground elevation", rx_bdens: "building density", rx_bldg_h: "building height at meter",
      obs_max_m: "max obstruction", obs_frac: "share of path blocked", fresnel_frac: "Fresnel zone blocked",
      bldg_cross: "buildings crossed", bldg_len_m: "metres through buildings", forest_len_m: "metres through forest",
      tx_h_agl: "antenna height", elev_angle_deg: "elevation angle", fspl_db: "free-space loss", terr_obs_frac: "terrain share", valid_frac: "data coverage",
      is_apa: "meter maker: Apator", is_axi: "meter maker: Axioma" };
    const P0 = I.plan, g = P0.greedy_coverage, nGw = existing.length;
    const NAMES = { physics: "Physics baseline (distance + height)", gbm: "Gradient boosting + line of sight",
      mlp: "Neural network + line of sight", catboost: "CatBoost + line of sight" };
    $("why-body").innerHTML = `
      <h2>Why some signals arrive and others don't</h2>
      <div class="kpis-why">
        <div class="kpi-why"><b>${I.meters_heard}/${I.meters_total}</b><span>water meters received in the measured week</span></div>
        <div class="kpi-why"><b>${I.excess_loss_db_under_500m == null ? "n/a" : "≈ " + Math.round(I.excess_loss_db_under_500m) + " dB"}</b><span>extra loss vs open air within 500 m – meters sit in basements</span></div>
        <div class="kpi-why"><b>${p0(I.redundancy.one)}</b><span>of received meters reach only one gateway</span></div>
      </div>
      <h4>Silent meters are mostly inactive – not dead zones</h4>
      <table><tr><th>Meter status</th><th>Meters</th><th>Received</th></tr>
      ${I.meter_status.map((r) => `<tr><td>${r.meter_state}</td><td>${r.meters}</td><td>${p0(r.heard_share)}</td></tr>`).join("")}</table>
      <p class="note">Only ${Math.round(st.A ? st.A.meters * (1 - st.A.heard_share) : 0)} active (A) meters were never heard – the real coverage holes.
      ${I.broken_coordinates_repaired} meters had corrupted coordinates in the source file and were re-located from their address.</p>
      <h4>Signal vs distance (received links)</h4>
      <svg viewBox="0 0 520 205" width="100%"><rect x="40" y="10" width="470" height="170" fill="#111419" rx="6"/>
        ${dots}<polyline points="${med}" fill="none" stroke="#f5a524" stroke-width="2.5"/>${xt}${yt}
        <text x="430" y="24">median</text><line x1="400" y1="21" x2="425" y2="21" stroke="#f5a524" stroke-width="2.5"/></svg>
      <p class="note">Most meters are only a few hundred metres from a gateway and still arrive close to the sensitivity limit.
      ${p0(I.sf_mix.filter((s) => s.sf >= 11).reduce((a, s) => a + s.share, 0))} of messages need SF11/12, which costs
      ${p0(I.sf_mix.filter((s) => s.sf >= 11).reduce((a, s) => a + s.airtime_share, 0))} of all airtime (battery). Median delivery ratio from frame counters: ${p0(I.delivery_ratio.median)}.</p>
      <h4>Does the model generalise? (honest validation)</h4>
      <table><tr><th>Model</th><th>New area<br><span class="src">spatial CV AUC</span></th><th>New gateway<br><span class="src">leave-one-out AUC</span></th><th>Brier</th></tr>
      ${["physics", "gbm", "mlp", "catboost"].filter((m) => cv("logo", m)).map((m) => `<tr${m === M.selected ? ' class="sel"' : ""}><td>${NAMES[m]}</td>
        <td>${cv("spatial", m).auc.toFixed(3)}</td><td>${cv("logo", m).auc.toFixed(3)}</td><td>${cv("logo", m).brier.toFixed(3)}</td></tr>`).join("")}</table>
      <p class="note">Signal strength error: <b>${M.rssi.mae_model.toFixed(1)} dB</b> vs ${M.rssi.mae_logdistance.toFixed(1)} dB for the textbook log-distance model.
      Trained on ${fmtN(M.n_links)} meter–gateway links (${M.n_devices} meters × ${nGw} gateways).</p>
      <h4>What drives reception</h4>
      ${imp.map(([f, v]) => `<div class="imp"><span>${label[f] || f}</span><i style="width:${Math.max(2, 230 * v / mx)}px"></i></div>`).join("")}
      <h4>Plan: best new sites (model coverage)</h4>
      <table><tr><th>#</th><th>Site</th><th>Coverage</th><th>Backup</th></tr>
      <tr><td>–</td><td>today (${nGw} gateways)</td><td>${p0(g.baseline.coverage)}</td><td>${p0(g.baseline.redundancy)}</td></tr>
      ${g.steps.map((s, k) => `<tr><td>${k + 1}</td><td>${s.site_id}</td><td>${p0(s.coverage)}</td><td>${p0(s.redundancy)}</td></tr>`).join("")}</table>
      <h4>Which existing gateway is most critical?</h4>
      <table><tr><th>If this gateway fails…</th><th>Coverage lost</th></tr>
      ${P0.failure.map((f) => `<tr><td>${f.name}</td><td>−${(100 * f.coverage_loss).toFixed(1)} pt</td></tr>`).join("")}</table>
      ${I.hnvg_docs ? (() => {
        const F = I.hnvg_docs, v = (k) => (F[k] ? F[k].value : null), Bc = I.business_case || {}, A = I.ablation_placement;
        const va = A ? A.variants.find((x) => x.variant.startsWith("A")) : null, vb = A ? A.variants.find((x) => x.variant === "B wall position, old features") : null;
        return `<h4>What HNVG's own documents add</h4>
        <table><tr><th>Rule (source)</th><th>How we use it</th></tr>
        <tr><td>Meter right behind the house entry; pipe straight from the street by the shortest route <span class="src">(TAB Wasser)</span></td>
            <td>tested: does a meter on the street-facing wall predict better? – no (see below)</td></tr>
        <tr><td>Meter shaft at the property line if the pipe on the property exceeds ${v("shaft_pipe_length_m")} m <span class="src">(Ergänzende Bedingungen AVBWasserV, 4)</span></td>
            <td>${fmtN(Bc.shaft_addresses || 0)} addresses flagged; reception ×${Bc.shaft_p_factor} (assumption – no shaft meter in the measured data)</td></tr>
        <tr><td>Remote-readable meters allowed; today read manually once a year <span class="src">(AVBWasserV conditions 9.2, 11.1)</span></td><td>business case below</td></tr>
        <tr><td>Manual check reading €${v("visit_cost_eur_net")} net, €${v("visit_cost_shaft_eur_net")} in a shaft <span class="src">(Montagepreise Wasserversorgung)</span></td>
            <td>value of avoided visits</td></tr>
        <tr><td>Drinking water €${v("water_price_eur_m3_net")}/m³ net <span class="src">(Preisblatt AVBWasserV)</span></td><td>daily readings catch leaks within days, not at the next annual reading</td></tr></table>
        ${Bc.today ? `<p class="note">Reference value of avoided manual visits per reading round: today ≈ <b>${eur(Bc.today.eur)}</b>
          (${fmtN(Bc.today.readable)} meters reachable) → with the best 3 new sites ≈ <b>${eur(Bc.plus3.eur)}</b> (${fmtN(Bc.plus3.readable)} meters).
          Assumes one water meter per address and uses HNVG's published check-reading price as a stand-in for the cost of a visit; HNVG's internal cost of routine reading will differ.</p>` : ""}
        ${va && vb ? `<p class="note"><b>Tested, not assumed:</b> moving each meter to the street-facing wall changed the new-gateway AUC from
          ${va.logo.toFixed(3)} to ${vb.logo.toFixed(3)} but made the signal error worse (${va.rssi_mae.toFixed(2)} → ${vb.rssi_mae.toFixed(2)} dB),
          and whether the wall faces the gateway does not matter (correlation ${A.facing_corr_rssi.toFixed(3)}) – in a basement the signal leaks out anyway.
          So the model keeps the address point; the documents feed the shaft flag and the business case.</p>` : ""}`;
      })() : ""}
      ${(() => {
        const H = I.hardware || {}, G = H.gateway || {}, MK = H.meters || {}, RO = I.radio_observed || {}, LB = I.link_budget || {};
        const BY = M.by_maker || {}, mix = (I.devices || {}).mix || {}, bands = ["(0, 500]", "(500, 1000]", "(1000, 2000]"];
        if (!G.model && !Object.keys(BY).length) return "";
        const SPEC = { 7: -7.5, 8: -10, 9: -12.5, 10: -15, 11: -17.5, 12: -20 }; // LoRa demodulation limits (Semtech)
        return `<h4>Hardware: gateway and meters</h4>
        ${G.model ? `<table><tr><th>Gateway</th><th>Datasheet (public)</th><th>Measured in the data</th></tr>
          <tr><td>Model</td><td colspan="2">${G.model}</td></tr>
          <tr><td>Receiver sensitivity</td><td>${G.rx_sensitivity_dbm_sf12} dBm (SF12)</td>
              <td>weakest received signals ≈ ${RO.rssi_floor_dbm != null ? RO.rssi_floor_dbm.toFixed(0) : "–"} dBm – limited by urban radio noise, not by the gateway</td></tr>
          <tr><td>Antenna, mounting</td><td>${G.antenna}; ${G.mounting}</td><td>gain assumed ${LB.gateway_antenna_dbi} dBi (not published)</td></tr>
          ${RO.snr_min_by_sf ? `<tr><td>Lowest SNR received</td><td>LoRa limit SF7 −7.5 … SF12 −20 dB</td>
              <td>${Object.entries(RO.snr_min_by_sf).map(([sf, v]) => `SF${sf} ${v.toFixed(1)}`).join(" · ")} dB – matches the limits${
                Object.entries(RO.snr_min_by_sf).every(([sf, v]) => v <= (SPEC[sf] ?? 0) + 3) ? "" : " (partly)"}</td></tr>` : ""}
        </table>` : ""}
        ${Object.keys(BY).length ? `<table style="margin-top:10px"><tr><th>Meter maker</th><th>Share</th><th>Received ≤ 500 m</th><th>500–1000 m</th><th>1–2 km</th></tr>
          ${Object.entries(BY).sort((a, b) => b[1].meters - a[1].meters).map(([m, x]) => `<tr${m === (I.devices || {}).weak ? ' class="sel"' : ""}>
            <td>${MK[m] ? MK[m].name : m}<br><span class="src">${MK[m] ? MK[m].radio : ""}</span></td>
            <td>${mix[m] != null ? p0(mix[m]) : x.meters}</td>
            ${bands.map((b) => `<td>${x.heard_by_distance[b] != null ? p0(x.heard_by_distance[b]) : "–"}</td>`).join("")}</tr>`).join("")}</table>
          <p class="note">The meter number tells the maker (8APA… = Apator). In the same 1 km blocks and at the same distance,
          Apator meters are heard clearly less often – most likely their internal antenna. The model therefore knows the maker,
          and the planner can plan for today's mix or, conservatively, for the weakest type.
          ${RO.msgs_per_meter_day ? `Meters send ≈ ${RO.msgs_per_meter_day.toFixed(1)} messages a day (${RO.payload_bytes ? Math.round(RO.payload_bytes) + " bytes, " : ""}adaptive data rate on).` : ""}</p>` : ""}`;
      })()}
      <h4>Data, assumptions &amp; limits</h4>
      <p class="note">Inputs: HNVG reception week ${I.week[0].slice(0, 10)} – ${I.week[1].slice(0, 10)}; LGL ALKIS, Hauskoordinaten and Basis-DLM, LoD1/LoD2 3D buildings,
      DOM1 surface model. Terrain: ${I.data.terrain_source}. Antenna heights are estimated from the surface model (highest structure + 2 m).
      "Coverage" means the probability that a <b>deep-indoor device like a water meter</b> is heard – street-level sensors will do better.
      The provider rule counts a device as served within ${DEV_R} m of a gateway; the measurements show reception often reaches further.
      Measured meters cluster in the west (Klingenberg/Böckingen); predictions far from them and for sites outside the 3D data are marked lower confidence.
      ${I.weather && I.weather.verdict ? `<b>Weather:</b> ${I.weather.verdict}` : "Weather: not analysed yet – put an Open-Meteo CSV into data/raw and run <code>uv run hnplan weather</code>."}</p>`;
  }

  // ---------- start ----------
  renderWhy();
  setGoal("coverage");
  setDevice("mix");
  setScenario("today");
  setPlanMode("target");
  setTarget(0.98);
  readHash();
  let started = false;
  const start = () => {
    if (started) return;
    started = true;
    refresh();
    $("loading").classList.add("done");
    setTimeout(() => $("loading").remove(), 500);
    // gentle intro: from a flat overview into the 3D city
    playLandmarks();
    // #view=lon,lat,zoom,pitch,bearing opens an exact camera position (screenshots, shared links)
    const v = (new URLSearchParams(location.hash.slice(1)).get("view") || "").split(",").map(Number);
    if (v.length >= 3 && v.every(Number.isFinite)) map.jumpTo({ center: [v[0], v[1]], zoom: v[2], pitch: v[3] || 55, bearing: v[4] || 0 });
    else if (S.cov.length) focusNewSites();
    else map.easeTo({ ...HOME, duration: 2600, easing: (t) => 1 - Math.pow(1 - t, 3) });
  };
  // landmark intro: full colour for `seconds`, then a short fade (layers are only drawn while S.lm > 0)
  let lmRun = 0;
  function playLandmarks() {
    if (!LM.length) return;
    const run = ++lmRun; // a replay (video) cancels a running intro
    const dur = Math.max(...LM.map((l) => l.seconds || 5)), calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
    let t0 = 0;
    const tickLm = (now) => {
      if (run !== lmRun) return;
      if (!t0) t0 = now;
      const e = (now - t0) / 1000;
      S.lm = e < dur ? 1 : Math.max(0, 1 - (e - dur) / 0.8);
      S.lmT = calm ? 0 : e;
      redraw();
      if (S.lm > 0) requestAnimationFrame(tickLm);
    };
    // the label is drawn into a font atlas once – wait (max 1.5 s) for the web font so it is not a fallback face
    Promise.race([document.fonts.load('600 13px "Geist"'), new Promise((r) => setTimeout(r, 1500))])
      .catch(() => {}).then(() => { S.lm = 1; requestAnimationFrame(tickLm); });
  }
  // radio links of the address cell nearest to lon/lat (what hovering a building shows)
  function showLinks(lon, lat) {
    let best = -1, bd = Infinity;
    for (let c = 0; c < nC; c++) { const d = dist(lon, lat, CL.lon[c], CL.lat[c]); if (d < bd) { bd = d; best = c; } }
    S.hoverCell = lon == null ? -1 : best; redraw();
    return best;
  }
  // planned buildings rise out of the ground (future city, used by the demo video)
  function growFuture(ms = 2500) {
    let t0 = 0;
    S.grow = 0.02; redraw();
    const step = (now) => {
      if (!t0) t0 = now;
      const t = Math.min(1, (now - t0) / ms);
      S.grow = 0.02 + 0.98 * (1 - Math.pow(1 - t, 3));
      redraw();
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  // backup demo (video): the address near lon/lat heard by the most active gateways (p > 0.5), and its strongest gateway
  function pickBackup(lon, lat, radiusM = 1500, linkM = 1800) {
    const act = active();
    let best = null;
    for (let c = 0; c < nC; c++) {
      const d = dist(lon, lat, CL.lon[c], CL.lat[c]);
      if (d > radiusM) continue;
      const hs = act.filter((s) => P[c * nS + s] > 0.5 && dist(CL.lon[c], CL.lat[c], sites[s].lon, sites[s].lat) < linkM).sort((a, b) => P[c * nS + b] - P[c * nS + a]);
      const score = Math.min(hs.length, 4) * 1e4 - d;
      if (hs.length >= 3 && (!best || score > best.score)) best = { score, lon: CL.lon[c], lat: CL.lat[c], n: hs.length, strongest: sites[hs[0]].id, slon: sites[hs[0]].lon, slat: sites[hs[0]].lat };
    }
    return best;
  }
  // "what if we built all approved sites?" – every candidate switched on
  function allSites() { S.plan = []; S.planInfo = null; S.extra = new Set(candidates); refresh(); writeHash(); }
  // automation hook for scripts/make_demo_video.py (no effect on normal use)
  window.__hn = {
    map, S, sites, setTarget, setPlanMode, setScenario, playLandmarks, showLinks, CITY, growFuture, allSites, pickBackup, setK, setGoal, setMode, refresh, focusNewSites, flyToSite,
    run: () => $("btn-opt").click(), reset: () => $("btn-reset").click(),
    toggle: (id) => { const s = sites.find((x) => x.id === id); if (s) toggleSite(s); },
    ready: () => started,
  };
  setStep("Rendering…");
  map.on("load", start);
  setTimeout(start, 7000);
})();
