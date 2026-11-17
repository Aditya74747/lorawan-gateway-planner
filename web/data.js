/* "Data & evidence" dashboard – every number behind the planner.
   Each section leads with its answer (figures + one or two charts); the full tables sit underneath
   ("Show table": sortable, searchable, CSV). Source: web/data/evidence.json (+ meters.json), written by
   `hnplan export` from the same objects the 3D map uses. */
(async function () {
  const $ = (id) => document.getElementById(id);
  let E, M;
  try {
    const get = async (n) => { const r = await fetch(`data/${n}.json`); if (!r.ok) throw new Error(`data/${n}.json – HTTP ${r.status}`); return r.json(); };
    [E, M] = await Promise.all([get("evidence"), get("meters")]);
  } catch (e) {
    $("doc-loading").innerHTML = `Could not load the data (${e.message}). Run <code>uv run hnplan export</code> and reload.`;
    return;
  }

  // ---------- formatting ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const n0 = (v) => (isNum(v) ? Math.round(v).toLocaleString("en-US") : "–");
  const n1 = (v) => (isNum(v) ? v.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : "–");
  const n2 = (v) => (isNum(v) ? v.toFixed(2) : "–");
  const n3 = (v) => (isNum(v) ? v.toFixed(3) : "–");
  const pc = (v, d = 1) => (isNum(v) ? (100 * v).toFixed(d) + " %" : "–");
  const sgn = (v, d = 2) => (isNum(v) ? (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d) : "–");
  const MAKER = { APA: "Apator", IMT: "IMT", AXI: "Axioma" };
  const pill = (txt, cls) => `<span class="pill ${cls || ""}">${esc(txt)}</span>`;
  const near = (a, b) => Math.abs(a - b) < 1e-6;
  const O = E.overview;
  const CITY = E.city || null; // another city (hnplan city <name>): no measurements, generated rooftop sites
  const APPROVED = CITY ? "proposed rooftop" : "approved";
  const plan = (t) => (E.plans || []).find((p) => near(p.target, t));
  const p90 = plan(0.9), p95 = plan(0.95), p98 = plan(0.98);
  if (CITY) {
    document.title = `${CITY.name} LoRaWAN Planner – Data & evidence`;
    document.querySelectorAll(".tb-brand b").forEach((e) => { e.textContent = `${CITY.name} LoRaWAN Planner`; });
    const keep = new Set(["#overview", "#plan", "#sites"]);
    document.querySelectorAll("#nav a").forEach((a) => { if (!keep.has(a.getAttribute("href"))) a.remove(); });
    $("nav").insertAdjacentHTML("beforeend", '<a href="#method">How it works</a>');
  }
  if (!E.future) document.querySelector('#nav a[href="#future"]')?.remove();
  if (!(E.weather && E.weather.rain)) document.querySelector('#nav a[href="#weather"]')?.remove();

  // ---------- shared tooltip (hover + keyboard focus on any [data-tip]) ----------
  const tip = document.createElement("div");
  tip.className = "vtip"; tip.setAttribute("role", "tooltip"); tip.hidden = true;
  document.body.appendChild(tip);
  const showTip = (html, x, y) => {
    tip.innerHTML = html; tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(x + 14, window.innerWidth - w - 8)) + "px";
    tip.style.top = Math.max(8, Math.min(y + 14, window.innerHeight - h - 8)) + "px";
  };
  const hideTip = () => { tip.hidden = true; };
  document.addEventListener("pointermove", (e) => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (t) showTip(t.dataset.tip, e.clientX, e.clientY); else if (!e.target.closest(".xhair-host")) hideTip();
  });
  document.addEventListener("focusin", (e) => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (t) { const r = t.getBoundingClientRect(); showTip(t.dataset.tip, r.left + 20, r.bottom); }
  });
  document.addEventListener("focusout", hideTip);
  const tipHtml = (value, label, rows = []) => `<div class="vt-v">${value}</div><div class="vt-l">${label}</div>` +
    (rows.length ? `<div class="vt-rows">${rows.map(([k, v]) => `<span>${k}</span><b>${v}</b>`).join("")}</div>` : "");
  const attr = (s) => esc(s); // tooltip HTML goes into an attribute

  // ---------- layout helpers ----------
  function section(id, title, lead) {
    const s = document.createElement("section");
    s.className = "doc-section"; s.id = id;
    s.innerHTML = `<h2>${title}</h2>${lead ? `<p class="lead">${lead}</p>` : ""}`;
    $("main").appendChild(s);
    return s;
  }
  const el = (html) => { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstElementChild; };
  function card(title, desc, body, cls = "") {
    return el(`<div class="ccard ${cls}"><div class="cc-head"><h3>${title}</h3>${desc ? `<p>${desc}</p>` : ""}</div>
      <div class="cc-body">${body}</div></div>`);
  }
  const row = (...els) => { const d = document.createElement("div"); d.className = "crow"; els.forEach((e) => d.appendChild(e)); return d; };
  const tile = (l, v, s, cls = "") => `<div class="kw ${cls}"><div class="l">${l}</div><div class="v">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`;

  // ---------- charts (plain SVG/HTML, one hue for magnitude, emphasis = accent vs grey) ----------
  /* Horizontal bars: one row per item, value at the bar tip. `hl(row)` = emphasis (others grey). */
  function hbars(rows, o) {
    const max = o.max ?? Math.max(...rows.map(o.value), 1e-9);
    return `<div class="hbars">${rows.map((r) => {
      const v = o.value(r), w = Math.max(0, Math.min(1, v / max));
      const on = o.hl ? o.hl(r) : true;
      return `<div class="hb${on ? " on" : ""}" tabindex="0" data-tip="${attr(o.tip ? o.tip(r) : tipHtml(o.fmt(v), esc(o.name(r))))}">
        <span class="hb-name">${o.label ? o.label(r) : esc(o.name(r))}</span>
        <span class="hb-track"><i style="width:${(100 * w).toFixed(2)}%"></i></span>
        <span class="hb-val">${o.fmt(v)}</span></div>`;
    }).join("")}</div>`;
  }

  /* Grouped horizontal bars: rows = groups, one thin bar per series, legend above. */
  function ghbars(groups, series, o) {
    const legend = `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>`;
    return legend + `<div class="ghbars">${groups.map((g) => `<div class="gh">
      <span class="gh-name">${esc(g.label)}</span>
      <div class="gh-bars">${series.map((s) => {
        const v = o.value(g, s);
        return `<div class="gh-bar" tabindex="0" data-tip="${attr(tipHtml(o.fmt(v), `${esc(s.name)} · ${esc(g.label)}`, o.rows ? o.rows(g, s) : []))}">
          <span class="hb-track"><i style="width:${isNum(v) ? (100 * Math.min(1, v / o.max)).toFixed(2) : 0}%;background:${s.color}"></i></span>
          <span class="hb-val">${isNum(v) ? o.fmt(v) : "–"}</span></div>`;
      }).join("")}</div></div>`).join("")}</div>`;
  }

  /* Coverage curve with crosshair: pts[k] = coverage with k new sites. marks = [{k, v, text}] */
  function curveChart(pts, marks, o = {}) {
    const W = 690, H = 300, L = 46, R = 16, T = 18, B = 40, n = pts.length - 1;
    const lo = Math.max(0, Math.floor(Math.min(...pts) * 10) / 10);
    const X = (k) => L + ((W - L - R) * k) / Math.max(n, 1), Y = (v) => H - B - ((H - T - B) * (v - lo)) / (1 - lo);
    const path = pts.map((v, k) => `${k ? "L" : "M"}${X(k).toFixed(1)},${Y(v).toFixed(1)}`).join("");
    const yt = [lo, lo + (1 - lo) / 2, 1].map((v) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/>
      <text class="ax" x="${L - 8}" y="${Y(v) + 4}" text-anchor="end">${Math.round(100 * v)} %</text>`).join("");
    const step = n > 140 ? 50 : n > 70 ? 20 : 10;
    const xt = Array.from({ length: Math.floor(n / step) + 1 }, (_, i) => i * step).map((k) =>
      `<text class="ax" x="${X(k)}" y="${H - B + 18}" text-anchor="middle">${k}</text>`).join("");
    const mk = marks.map((m) => `<circle class="mk" cx="${X(m.k)}" cy="${Y(m.v)}" r="5"/>
      <text class="mk-l" x="${Math.min(X(m.k) + 10, W - R - 120)}" y="${Y(m.v) + (m.dy ?? 18)}">${esc(m.text)}</text>`).join("");
    const id = "c" + Math.random().toString(36).slice(2, 8);
    const host = el(`<div class="xhair-host"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${attr(o.aria || "Coverage curve")}">
      ${yt}${xt}<text class="ax" x="${W - R}" y="${H - 6}" text-anchor="end">${esc(o.xLabel || "new gateways")}</text>
      <line class="base" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>
      <path class="area" d="${path}L${X(n)},${H - B}L${X(0)},${H - B}Z"/><path class="line" d="${path}"/>
      ${o.target ? `<line class="tline" x1="${L}" x2="${W - R}" y1="${Y(o.target)}" y2="${Y(o.target)}"/>` : ""}
      ${mk}
      <line id="${id}-x" class="xhair" x1="0" x2="0" y1="${T}" y2="${H - B}" visibility="hidden"/>
      <circle id="${id}-d" class="xdot" r="5" visibility="hidden"/>
      <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}"/></svg></div>`);
    const svg = host.querySelector("svg"), xl = host.querySelector(`#${id}-x`), xd = host.querySelector(`#${id}-d`);
    host.querySelector(".hit").addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect(), sx = ((e.clientX - r.left) / r.width) * W;
      const k = Math.max(0, Math.min(n, Math.round(((sx - L) / (W - L - R)) * n)));
      xl.setAttribute("x1", X(k)); xl.setAttribute("x2", X(k)); xl.setAttribute("visibility", "visible");
      xd.setAttribute("cx", X(k)); xd.setAttribute("cy", Y(pts[k])); xd.setAttribute("visibility", "visible");
      showTip(tipHtml(pc(pts[k]), k ? `with ${k} new gateway${k > 1 ? "s" : ""} (build order)` : "today", o.rows ? o.rows(k) : []), e.clientX, e.clientY);
    });
    host.querySelector(".hit").addEventListener("pointerleave", () => { xl.setAttribute("visibility", "hidden"); xd.setAttribute("visibility", "hidden"); hideTip(); });
    return host;
  }

  // round an axis maximum up to 1/2/2.5/5 × 10^n  (118 -> 120, 0.83 -> 1)
  const niceMax = (v) => { const e = 10 ** Math.floor(Math.log10(v)), f = v / e; return (f <= 1 ? 1 : f <= 1.2 ? 1.2 : f <= 1.5 ? 1.5 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 3 ? 3 : f <= 4 ? 4 : f <= 5 ? 5 : f <= 6 ? 6 : f <= 8 ? 8 : 10) * e; };
  /* Columns (ordered bins, e.g. a histogram). */
  function columns(rows, o) {
    const W = 530, H = 230, L = 38, R = 10, T = 20, B = 38, n = rows.length;
    const max = o.max ?? niceMax(Math.max(...rows.map(o.value), 1e-9));
    const slot = (W - L - R) / n, bw = Math.min(24, slot * 0.6);
    const Y = (v) => H - B - ((H - T - B) * v) / max;
    const bars = rows.map((r, i) => {
      const v = o.value(r), x = L + slot * i + (slot - bw) / 2, y = Y(v), h = H - B - y, rr = Math.min(4, h);
      const d = h > 0 ? `M${x},${H - B}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + bw - rr}Q${x + bw},${y} ${x + bw},${y + rr}V${H - B}Z` : "";
      return `<g tabindex="0" data-tip="${attr(tipHtml(o.fmt(v), esc(o.name(r))))}"><rect class="hit" x="${L + slot * i}" y="${T}" width="${slot}" height="${H - T - B}"/>
        <path class="${o.hl && !o.hl(r) ? "col off" : "col"}" d="${d}"/>
        ${o.labelAll || (o.labelIf && o.labelIf(r)) ? `<text class="cv" x="${x + bw / 2}" y="${y - 5}" text-anchor="middle">${o.fmt(v)}</text>` : ""}
        <text class="ax" x="${x + bw / 2}" y="${H - B + 14}" text-anchor="middle">${esc(o.tick(r, i))}</text></g>`;
    }).join("");
    return `<svg class="cols" viewBox="0 0 ${W} ${H}" role="img" aria-label="${attr(o.aria || "")}">
      <line class="grid" x1="${L}" x2="${W - R}" y1="${Y(max / 2)}" y2="${Y(max / 2)}"/><text class="ax" x="${L - 6}" y="${Y(max / 2) + 3}" text-anchor="end">${o.axFmt(max / 2)}</text>
      <line class="grid" x1="${L}" x2="${W - R}" y1="${Y(max)}" y2="${Y(max)}"/><text class="ax" x="${L - 6}" y="${Y(max) + 3}" text-anchor="end">${o.axFmt(max)}</text>
      <line class="base" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>${bars}
      ${o.xLabel ? `<text class="ax" x="${W - R}" y="${H - 4}" text-anchor="end">${esc(o.xLabel)}</text>` : ""}</svg>`;
  }

  /* Dot plot with a free axis (for values that only differ in the 2nd decimal, e.g. AUC). */
  function dotplot(rows, o) {
    const W = 530, rowH = 26, L = 170, R = 50, T = 8, H = T + rows.length * rowH + 26;
    const [a, b] = o.domain, X = (v) => L + ((W - L - R) * (v - a)) / (b - a);
    const ticks = o.ticks.map((v) => `<line class="grid" x1="${X(v)}" x2="${X(v)}" y1="${T}" y2="${H - 22}"/>
      <text class="ax" x="${X(v)}" y="${H - 8}" text-anchor="middle">${o.tickFmt(v)}</text>`).join("");
    const pts = rows.map((r, i) => {
      const v = o.value(r), y = T + i * rowH + rowH / 2, on = o.hl(r);
      return `<g tabindex="0" data-tip="${attr(o.tip(r))}"><rect class="hit" x="0" y="${y - rowH / 2}" width="${W}" height="${rowH}"/>
        <text class="${on ? "dn on" : "dn"}" x="${L - 12}" y="${y + 4}" text-anchor="end">${esc(o.name(r))}</text>
        <line class="stem" x1="${L}" x2="${X(Math.max(a, v))}" y1="${y}" y2="${y}"/>
        <circle class="${on ? "dot on" : "dot"}" cx="${X(Math.max(a, v))}" cy="${y}" r="5"/>
        <text class="cv" x="${X(Math.max(a, v)) + 10}" y="${y + 4}">${o.fmt(v)}</text></g>`;
    }).join("");
    return `<svg class="dots" viewBox="0 0 ${W} ${H}" role="img" aria-label="${attr(o.aria || "")}">${ticks}${pts}</svg>`;
  }

  /* Effect sizes with 95 % intervals against zero (forest plot). Grey = interval contains 0 (no measurable effect). */
  function forest(rows, o) {
    const W = 1110, rowH = 38, L = 230, R = 190, T = 10, H = T + rows.length * rowH + 30;
    const ext = Math.max(...rows.flatMap((r) => [Math.abs(r.lo), Math.abs(r.hi)]), 0.1) * 1.15;
    const X = (v) => L + ((W - L - R) * (v + ext)) / (2 * ext);
    const tk = [-ext, -ext / 2, 0, ext / 2, ext].map((v) => Math.round(v * 100) / 100);
    const ticks = tk.map((v) => `<line class="${v === 0 ? "zero" : "grid"}" x1="${X(v)}" x2="${X(v)}" y1="${T}" y2="${H - 24}"/>
      <text class="ax" x="${X(v)}" y="${H - 8}" text-anchor="middle">${v === 0 ? "0" : sgn(v)} ${esc(o.unit)}</text>`).join("");
    const items = rows.map((r, i) => {
      const y = T + i * rowH + rowH / 2, eff = r.lo > 0 || r.hi < 0;
      return `<g tabindex="0" data-tip="${attr(tipHtml(`${sgn(r.value)} ${esc(o.unit)}`, esc(r.label), [["95 % interval", `${sgn(r.lo)} … ${sgn(r.hi)} ${esc(o.unit)}`],
        ["Verdict", eff ? "measurable effect" : "no measurable effect"]]))}">
        <rect class="hit" x="0" y="${y - rowH / 2}" width="${W}" height="${rowH}"/>
        <text class="dn on" x="${L - 16}" y="${y + 4}" text-anchor="end">${esc(r.label)}</text>
        <line class="${eff ? "ci on" : "ci"}" x1="${X(r.lo)}" x2="${X(r.hi)}" y1="${y}" y2="${y}"/>
        <circle class="${eff ? "dot on" : "dot"}" cx="${X(r.value)}" cy="${y}" r="5"/>
        <text class="cv" x="${W - R + 14}" y="${y + 4}">${eff ? "effect" : "no effect"} · ${sgn(r.value)} ${esc(o.unit)}</text></g>`;
    }).join("");
    return `<svg class="forest" viewBox="0 0 ${W} ${H}" role="img" aria-label="${attr(o.aria || "")}">${ticks}${items}</svg>`;
  }

  /* Before → after per row (dumbbell): sites needed today vs in the future city. */
  function dumbbell(rows, o) {
    const W = 1110, rowH = 44, L = 120, R = 40, T = 10, H = T + rows.length * rowH + 30;
    const max = Math.max(...rows.flatMap((r) => [o.a(r), o.b(r)])) * 1.1, X = (v) => L + ((W - L - R) * v) / max;
    const step = max > 60 ? 10 : 5;
    const ticks = Array.from({ length: Math.floor(max / step) + 1 }, (_, i) => i * step).map((v) => `<line class="grid" x1="${X(v)}" x2="${X(v)}" y1="${T}" y2="${H - 24}"/>
      <text class="ax" x="${X(v)}" y="${H - 8}" text-anchor="middle">${v}</text>`).join("");
    const items = rows.map((r, i) => {
      const y = T + i * rowH + rowH / 2, a = o.a(r), b = o.b(r);
      return `<g tabindex="0" data-tip="${attr(o.tip(r))}"><rect class="hit" x="0" y="${y - rowH / 2}" width="${W}" height="${rowH}"/>
        <text class="dn on" x="${L - 16}" y="${y + 4}" text-anchor="end">${esc(o.name(r))}</text>
        <line class="stem" x1="${X(a)}" x2="${X(b)}" y1="${y}" y2="${y}"/>
        <circle class="dot" cx="${X(a)}" cy="${y}" r="6"/><circle class="dot on" cx="${X(b)}" cy="${y}" r="6"/>
        <text class="cv" x="${X(a)}" y="${y - 12}" text-anchor="middle">${a}</text>
        <text class="cv strong" x="${X(b)}" y="${y - 12}" text-anchor="middle">${b}</text></g>`;
    }).join("");
    return `<div class="legend"><span><i class="dotkey"></i>${esc(o.aName)}</span><span><i class="dotkey on"></i>${esc(o.bName)}</span></div>
      <svg class="dumbbell" viewBox="0 0 ${W} ${H}" role="img" aria-label="${attr(o.aria || "")}">${ticks}${items}</svg>`;
  }

  // ---------- generic table: sort, search, filter, CSV; collapsed behind "Show table" by default ----------
  function table(cfg) {
    const st = { key: cfg.sort?.[0] ?? null, dir: cfg.sort?.[1] ?? -1, q: "", f: cfg.filter?.options?.[0]?.[0] ?? "" };
    const host = document.createElement(cfg.open ? "div" : "details");
    host.className = "tcard" + (cfg.open ? "" : " tdet");
    const head = `<div class="tcard-head"><div><h3>${cfg.title}</h3>${cfg.desc ? `<p>${cfg.desc}</p>` : ""}</div>
      <div class="tools">
        ${cfg.filter ? `<select aria-label="Filter">${cfg.filter.options.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>` : ""}
        ${cfg.search ? `<input type="search" placeholder="Search…" aria-label="Search ${esc(cfg.title)}">` : ""}
        <button class="btn-sm" data-csv>CSV ↓</button>
      </div></div>`;
    host.innerHTML = (cfg.open ? head : `<summary><span class="sum-t">${cfg.title}</span><span class="sum-n">${cfg.rows.length} rows</span><span class="sum-a">Show table</span></summary>${head}`) +
      `<div class="tscroll" style="${cfg.maxH ? `max-height:${cfg.maxH}px` : ""}"><table class="dt"><thead><tr>${cfg.cols.map((c, i) =>
        `<th class="${c.num ? "num" : ""}" data-i="${i}">${c.label}<span class="arr"></span></th>`).join("")}</tr></thead><tbody></tbody></table></div>
      ${cfg.foot ? `<div class="tfoot">${cfg.foot}</div>` : ""}`;
    const tbody = host.querySelector("tbody"), ths = [...host.querySelectorAll("th")];
    const val = (c, r) => (c.sort ? c.sort(r) : r[c.k]);
    function rows() {
      let out = cfg.rows.filter((r) => (!cfg.filter || cfg.filter.test(r, st.f)) &&
        (!st.q || JSON.stringify(Object.values(r)).toLowerCase().includes(st.q)));
      if (st.key != null) {
        const c = cfg.cols[st.key];
        out = [...out].sort((a, b) => {
          const x = val(c, a), y = val(c, b), xn = x == null || x === "", yn = y == null || y === "";
          if (xn || yn) return xn - yn; // empty values always last
          return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * st.dir;
        });
      }
      return out;
    }
    function render() {
      const rs = rows();
      tbody.innerHTML = rs.map((r) => `<tr class="${cfg.rowClass ? cfg.rowClass(r) : ""}">${cfg.cols.map((c) => {
        const v = r[c.k], html = c.fmt ? c.fmt(v, r) : v == null ? "–" : esc(v);
        return `<td class="${c.num ? "num" : ""}${v == null && !c.fmt ? " muted" : ""}">${html}</td>`;
      }).join("")}</tr>`).join("") || `<tr><td colspan="${cfg.cols.length}" class="muted">No rows match.</td></tr>`;
      ths.forEach((th, i) => { th.querySelector(".arr").textContent = st.key === i ? (st.dir > 0 ? "▲" : "▼") : ""; });
    }
    ths.forEach((th, i) => (th.onclick = () => { st.dir = st.key === i ? -st.dir : cfg.cols[i].num ? -1 : 1; st.key = i; render(); }));
    const s = host.querySelector("input[type=search]");
    if (s) s.oninput = () => { st.q = s.value.trim().toLowerCase(); render(); };
    const f = host.querySelector("select");
    if (f) f.onchange = () => { st.f = f.value; render(); };
    host.querySelector("[data-csv]").onclick = () => {
      const q = (x) => { const t = String(x ?? ""); return /[",\n;]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
      const lines = [cfg.cols.map((c) => q(c.label.replace(/<[^>]+>/g, ""))).join(",")]
        .concat(rows().map((r) => cfg.cols.map((c) => q(c.csv ? c.csv(r[c.k], r) : r[c.k])).join(",")));
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" }));
      a.download = `${cfg.file || "table"}.csv`; a.click(); URL.revokeObjectURL(a.href);
    };
    render();
    return host;
  }
  $("doc-loading").remove();

  // =====================================================================================================
  // 1. Overview – the answer first, then the key findings (each links to its evidence)
  // =====================================================================================================
  const week = (O.week || []).map((d) => String(d).slice(0, 10)).join(" – ");
  const FUT = E.future && E.future.plans ? E.future.plans.find((p) => near(p.target, 0.98)) : null;
  const s1 = section("overview", CITY ? `${esc(CITY.name)}: how many gateways are needed?` : "How many new gateways does Heilbronn need?", "");
  s1.insertAdjacentHTML("beforeend", `<div class="hero">
    <div class="hero-main">
      <div class="hero-k">Fewest ${APPROVED} sites for 98 % coverage</div>
      <div class="hero-num">${p98?.k_mix ?? "–"}<span>of ${n0(O.candidates)}</span></div>
      <p class="hero-text">${CITY
        ? `${n0(p98?.k_mix)} rooftops – proposed automatically, no operator data – are enough to hear 98&nbsp;% of ${n0(O.addresses)} water meters in ${esc(CITY.name)}.`
        : `${n0(p98?.k_mix)} of HNVG's ${n0(O.candidates)} approved sites lift the expected coverage from <b>${pc(O.coverage_today, 0)}</b> today to <b>98&nbsp;%</b>.
           The other <b>${n0(O.candidates - (p98?.k_mix || 0))}</b> sites are not needed for that target.`}</p>
      <div class="meterbar" role="img" aria-label="Coverage today ${pc(O.coverage_today, 0)}, with the plan 98 %">
        <span class="mb-now" style="width:${(100 * (O.coverage_today || 0)).toFixed(1)}%"></span><span class="mb-plan" style="width:${(98 - 100 * (O.coverage_today || 0)).toFixed(1)}%"></span></div>
      <div class="mb-keys"><span><i class="k-now"></i>${O.existing ? `today, ${n0(O.existing)} gateways` : "no gateways today"}: ${pc(O.coverage_today, 0)}</span><span><i class="k-plan"></i>with the plan: 98 %</span></div>
    </div>
    <div class="hero-tiles">
      ${tile("For 90 % / 95 %", `${p90?.k_mix ?? "–"} / ${p95?.k_mix ?? "–"}`, "sites needed")}
      ${tile("If every meter were the weakest type", p98?.k_weak ?? "–", `sites for 98 % (${esc(MAKER[E.devices?.weak] || E.devices?.weak || "–")})`)}
      ${tile(`All ${n0(O.candidates)} sites together`, pc(O.coverage_all), "the most the sites can reach")}
      ${CITY ? tile("Addresses = meters", n0(O.addresses), `${n0(O.buildings)} 3D buildings`) : tile("Within 300 m today", pc(O.within300_today, 0), "HNVG's rule of thumb – reception reaches further")}
      ${tile(CITY ? "Model (trained in Heilbronn)" : "Model on gateways it never saw", `AUC ${n3(O.logo_auc)}`, "1 = perfect, 0.5 = coin flip")}
      ${CITY ? tile("Rooftop sites proposed", n0(O.candidates), `highest roof per ${n0(CITY.spacing_m)} m square`) : tile("Measured water meters heard", `${n0(O.meters_heard)} / ${n0(O.meters)}`, `week ${esc(week)}`)}
    </div></div>`);

  if (!CITY) {
    const RBD = E.reception_by_distance || [], b0 = RBD[0], b3 = RBD.find((r) => r.band.startsWith("1000"));
    const bm = E.by_maker || {}, apa = bm.APA?.heard_by_distance?.["(500, 1000]"], imt = bm.IMT?.heard_by_distance?.["(500, 1000]"];
    const rain = E.weather?.rain?.rssi_db;
    const best = E.benchmark && E.benchmark.length ? E.benchmark.length : null;
    const F = [
      { t: `${p98?.k_mix} of ${O.candidates} sites reach 98 %`, d: `The first ${p95?.k_mix} sites already give 95 %; each further site adds less. The greedy build order is then pruned and checked for 2-for-1 swaps.`, h: "#plan" },
      { t: "Reception reaches well beyond 300 m", d: `Measured: ${pc(b0?.heard, 0)} of links heard within 250 m, ${pc(b3?.heard, 0)} at 1–2 km. Only ${pc(O.within300_today, 0)} of addresses are within 300 m of a gateway, yet ≈ ${pc(O.coverage_today, 0)} are covered.`, h: "#measured" },
      { t: "The meter maker matters", d: `At 500–1000 m, ${pc(imt, 0)} of IMT links but only ${pc(apa, 0)} of Apator links are heard. Planning for the weakest type needs ${p98?.k_weak} instead of ${p98?.k_mix} sites.`, h: "#measured" },
      { t: "The model is tested on unseen gateways", d: `Leave-one-gateway-out AUC ${n3(O.logo_auc)}${best ? `, best of ${best} learners` : ""}; signal error ${n1(O.rssi_mae)} dB vs ${n1(O.rssi_mae_textbook)} dB for the textbook formula.`, h: "#model" },
      ...(rain ? [{ t: "Rain has no measurable effect", d: `Signal in rain ${sgn(rain.value)} dB (95 % interval ${sgn(rain.lo)} … ${sgn(rain.hi)}). Snow, frost and summer foliage were not in the measured week.`, h: "#weather" }] : []),
      ...(FUT ? [{ t: "Future districts need a few more sites", d: `With the land-use plans' new districts, 98 % needs ${FUT.k_future} instead of ${FUT.k_today} sites; today's plan would drop to ${pc(FUT.today_plan_future)}.`, h: "#future" }] : []),
    ];
    s1.insertAdjacentHTML("beforeend", `<h3 class="sub">Key findings</h3><div class="findings">${F.map((f, i) => `<a class="finding" href="${f.h}">
      <span class="fi-n">${i + 1}</span><div><b>${f.t}</b><p>${f.d}</p><span class="fi-go">See the evidence →</span></div></a>`).join("")}</div>
      <p class="scale">Based on ${n0(O.addresses)} addresses · ${n0(O.buildings)} 3D buildings · ${n0(O.receptions)} radio receptions from ${n0(O.meters_heard)} meters ·
      ${n0(O.train_links)} measured meter–gateway links · ${n0(O.pred_links)} predicted links. Every table below can be downloaded as CSV.</p>`);
  } else {
    const M2 = CITY.model || {};
    const F = [
      { t: `${p98?.k_mix} rooftops reach 98 %`, d: `${p95?.k_mix} are enough for 95 %. The rooftops are proposed by the planner: the highest roof in every ${n0(CITY.spacing_m)} m square that has addresses.`, h: "#plan" },
      { t: "No operator data needed", d: `${n0(CITY.addresses)} official house coordinates are the meters; ${n0(CITY.buildings)} LGL 3D buildings, surface and terrain models are the obstacles.`, h: "#method" },
      { t: "Model trained on real measurements", d: `Trained on HNVG's Heilbronn week, without absolute ground elevation (it does not transfer). AUC ${n3(M2.logo_auc)} on unseen gateways vs ${n3(M2.heilbronn_logo_auc)} with it.`, h: "#method" },
    ];
    s1.insertAdjacentHTML("beforeend", `<h3 class="sub">Key findings</h3><div class="findings">${F.map((f, i) => `<a class="finding" href="${f.h}">
      <span class="fi-n">${i + 1}</span><div><b>${f.t}</b><p>${f.d}</p><span class="fi-go">See the evidence →</span></div></a>`).join("")}</div>`);
  }

  // =====================================================================================================
  // 2. Plan – coverage curve + fewest sites per target
  // =====================================================================================================
  const s3 = section("plan", `Plan: how few of the ${APPROVED} sites are enough?`,
    `Sites are added in the order that gains the most addresses each time (greedy – provably within 63 % of the best possible set,
     usually optimal). For a target we take the shortest prefix that reaches it, then drop sites the others make redundant and test whether
     two sites can be replaced by one.`);
  const C = E.curve || [], targets = [0.9, 0.95, 0.98].map(plan).filter(Boolean);
  const curve = curveChart(C, targets.map((p, i) => ({ k: p.k_mix, v: p.coverage_mix, text: `${Math.round(p.target * 100)} % → ${p.k_mix} sites`, dy: [36, 20, -12][i] ?? 18 })),
    { aria: "Coverage versus number of new gateways", target: 0.98,
      rows: (k) => [["Addresses covered", `≈ ${n0((C[k] || 0) * O.addresses)}`], ["Gain from this site", k ? `+${n2(100 * (C[k] - C[k - 1]))} pt` : "–"]] });
  const tcard = card("Coverage vs number of new gateways", "Hover the curve for each step. Dots = the fewest-site plans for 90 / 95 / 98 %.", "", "wide");
  tcard.querySelector(".cc-body").appendChild(curve);
  const steps = card("Fewest sites per target", `Today's meter mix vs every meter like the weakest type (${esc(MAKER[E.devices?.weak] || E.devices?.weak || "–")}).`,
    `<div class="tgt">${(E.plans || []).map((p) => `<div class="tg-row${near(p.target, 0.98) ? " on" : ""}">
      <span class="tg-t">${pc(p.target, 0)}</span>
      <span class="tg-k"><b>${p.k_mix ?? "–"}</b> sites</span>
      <span class="tg-w">${p.k_weak ?? "–"} weakest</span></div>`).join("")}</div>
     <p class="cc-note">Greedy alone would need ${(E.plans || []).filter((p) => p.greedy_k && p.k_mix && p.greedy_k > p.k_mix).length ? "more for some targets – pruning saved sites" : "the same number – the greedy order was already minimal"}.</p>`, "narrow");
  s3.appendChild(row(tcard, steps));
  s3.appendChild(table({
    title: "Build order per target", file: "min-sites-plan", sort: [0, 1],
    cols: [
      { k: "target", label: "Target", num: true, fmt: (v) => pc(v, 0) },
      { k: "k_mix", label: "Sites (today's mix)", num: true, fmt: (v) => (v == null ? "not reachable" : `<b>${v}</b>`) },
      { k: "coverage_mix", label: "Coverage reached", num: true, fmt: (v) => pc(v, 2) },
      { k: "k_weak", label: "Sites (weakest type)", num: true, fmt: (v) => v ?? "not reachable" },
      { k: "greedy_k", label: "Greedy alone", num: true, fmt: (v) => v ?? "–" },
      { k: "sites", label: "Build order (most valuable first)", fmt: (v) => (v || []).map((x) => esc(x)).join(", "), csv: (v) => (v || []).join(" ") },
    ],
    foot: "Build order = the order to install the sites; each one adds the most coverage on top of the ones before it.",
    rows: E.plans || [],
  }));

  // =====================================================================================================
  // 3. Sites – which sites matter most
  // =====================================================================================================
  const S = E.sites || [];
  const MOUNT = { roof: "building roof", structure: "tower / structure", "free mast": "free-standing mast" };
  const s2 = section("sites", "Gateway sites: which ones matter?",
    `Every ${CITY ? "proposed rooftop" : "existing gateway and approved candidate"} with its assumed mounting (tallest roof within 30 m + 2 m,
     else a tower or structure, else a 10 m mast), the addresses it can reach and its place in the plans.`);
  const byId = Object.fromEntries(S.map((r) => [r.id, r]));
  const gp = (p98 && p98.steps && p98.steps.length > 1 ? p98 : null);
  const gains = gp ? gp.sites.map((id, i) => ({ id, i: i + 1, g: gp.steps[i + 1] - gp.steps[i], cum: gp.steps[i + 1], s: byId[id] || {} })) : [];
  const SHOW = 20, shown = gains.slice(0, SHOW);
  const candCard = card("The 98 % plan, site by site", `What each site adds on top of the ones built before it, in build order${gains.length > SHOW ? ` (first ${SHOW} of ${gains.length})` : ""}.
      Gains shrink because later sites mostly overlap areas that are already covered.`,
    hbars(shown, { name: (r) => r.id, value: (r) => r.g, fmt: (v) => `+${n1(100 * v)} pt`,
      label: (r) => `<span class="rk">${r.i}</span>${esc(r.id)}`,
      tip: (r) => tipHtml(`+${n1(100 * r.g)} pt`, `${esc(r.id)} – site ${r.i} of the build order`, [["Coverage after this site", pc(r.cum)],
        ["Addresses newly covered", `≈ ${n0(r.g * O.addresses)}`], ["Antenna", `${n1(r.s.antenna_m)} m (${esc(MOUNT[r.s.mount] || r.s.mount || "–")})`],
        ["Confidence", esc(r.s.confidence || "–")]]) }) +
    `<p class="cc-note">A site that is strong on its own can still be left out when its area is already covered – the full table below also lists each site's value on its own.</p>`);
  const ex = S.filter((r) => r.kind === "existing" && isNum(r.fail_loss_pt)).sort((a, b) => b.fail_loss_pt - a.fail_loss_pt);
  const exCard = ex.length ? card("If an existing gateway fails", "Coverage lost if this gateway went offline – the critical ones have no backup nearby.",
    hbars(ex, { name: (r) => r.name.replace(/^Heilbronn[-\s]+/, ""), value: (r) => r.fail_loss_pt, fmt: (v) => `−${n1(v)} pt`,
      tip: (r) => tipHtml(`−${n1(r.fail_loss_pt)} pt`, esc(r.name), [["Meters heard (week)", n0(r.meters_heard)], ["Receptions", n0(r.receptions)], ["Antenna", `${n1(r.antenna_m)} m`]]) })) : null;
  s2.appendChild(exCard ? row(candCard, exCard) : candCard);
  s2.appendChild(table({
    title: `All ${S.length} sites`, file: "gateway-sites", search: true, maxH: 560,
    desc: "Click a column to sort. Ranks = position in the build order of the fewest-site plan for that target (empty = not needed).",
    filter: { options: [["all", "All sites"], ...(CITY ? [] : [["existing", "Existing gateways"], ["candidate", "Candidate sites"]]), ["p95", "In the 95 % plan"], ["p98", "In the 98 % plan"]],
      test: (r, f) => f === "all" || r.kind === f || (f === "p95" && r.rank_95) || (f === "p98" && r.rank_98) },
    sort: [9, -1], rowClass: (r) => (r.rank_95 ? "hl" : ""),
    cols: [
      { k: "kind", label: "Type", fmt: (v) => pill(v === "existing" ? "existing" : "candidate", v === "existing" ? "ex" : "cand") },
      { k: "id", label: "Site", fmt: (v, r) => `<b>${esc(r.kind === "existing" ? r.name : v)}</b>${r.kind === "existing" ? `<br><span class="quote">${esc(v)}</span>` : ""}` },
      { k: "mount", label: "Mounting (assumed)", fmt: (v) => esc(MOUNT[v] || v || "–") },
      { k: "antenna_m", label: "Antenna m", num: true, fmt: n1 },
      { k: "ground_m", label: "Ground m a.s.l.", num: true, fmt: n1 },
      { k: "confidence", label: "Confidence", fmt: (v) => pill(v, v), sort: (r) => ({ high: 3, medium: 2, low: 1 }[r.confidence] || 0) },
      { k: "addr_alone", label: "Addresses alone", num: true, fmt: n0 },
      { k: "addr_reach50", label: "Reach ≥ 50 %", num: true, fmt: n0 },
      { k: "addr_300m", label: "Within 300 m", num: true, fmt: n0 },
      { k: "addr_new", label: "New vs today", num: true, fmt: n0 },
      { k: "rank_90", label: "Rank 90 %", num: true, fmt: (v) => v ?? "–" },
      { k: "rank_95", label: "Rank 95 %", num: true, fmt: (v) => v ?? "–" },
      { k: "rank_98", label: "Rank 98 %", num: true, fmt: (v) => v ?? "–" },
      ...(CITY ? [] : [
        { k: "fail_loss_pt", label: "If it fails", num: true, fmt: (v) => (v == null ? "–" : `−${n1(v)} pt`) },
        { k: "receptions", label: "Receptions", num: true, fmt: n0 },
        { k: "meters_heard", label: "Meters heard", num: true, fmt: n0 },
        { k: "rssi_med", label: "RSSI median", num: true, fmt: (v) => (v == null ? "–" : `${n0(v)} dBm`) }]),
    ],
    foot: `<b>Addresses alone</b> = Σ addresses × predicted probability for this site on its own. <b>Reach ≥ 50 %</b> = addresses where the site hears a meter
      with ≥ 50 % probability. <b>Within 300 m</b> = provider rule. <b>New vs today</b> = expected addresses not yet covered by today's gateways.
      <b>Confidence</b>: lower where the site lies outside the 3D data or far from the measured conditions.`,
    rows: S,
  }));

  // =====================================================================================================
  // City: how it works, then stop (no measurements for other cities)
  // =====================================================================================================
  if (CITY) {
    const C2 = CITY, M2 = C2.model || {};
    const sm = section("method", `How it works for ${esc(C2.name)}`, `The same pipeline as for Heilbronn, run only on open data of ${esc(C2.name)} – no operator data needed.`);
    sm.appendChild(row(
      card("Inputs", "", `<dl class="facts">
        <dt>3D city</dt><dd>${n0(C2.buildings)} LGL LoD2 buildings, DOM1 surface and DGM1 terrain on a 5 m grid</dd>
        <dt>Meters</dt><dd>${n0(C2.addresses)} official house coordinates, one basement meter per address, in ${n0(C2.cells)} cells of ${n0(C2.cell_m || 50)} m</dd>
        <dt>Gateway sites</dt><dd>${n0(C2.candidates)} rooftops: highest roof (ground + building height) per ${n0(C2.spacing_m)} m square with addresses; antenna roof + 2 m (median ${n0(C2.antenna_median_m)} m)</dd>
        <dt>Existing gateways</dt><dd>${C2.existing ? `${C2.existing} from raw/gateways.csv` : "none given (add raw/gateways.csv: name;lat;lon)"}</dd></dl>`),
      card("Reception model", "", `<dl class="facts">
        <dt>Trained on</dt><dd>HNVG's measured Heilbronn week, ${n0(M2.n_links)} meter–gateway links</dd>
        <dt>Left out</dt><dd>absolute ground elevation – Heilbronn lies at 157–218 m, other cities elsewhere; it does not transfer</dd>
        <dt>Validation</dt><dd>new-gateway AUC ${n3(M2.logo_auc)} (with elevation ${n3(M2.heilbronn_logo_auc)}); log-loss ${n3(M2.logo_logloss)} vs ${n3(M2.heilbronn_logo_logloss)}</dd>
        <dt>Limits</dt><dd>${pc(C2.extrap_share, 0)} of radio paths lie partly outside the Heilbronn range; the model then holds the nearest known value. Next step: one week of measurements with 2–3 test gateways.</dd></dl>`)));
    finishNav();
    return;
  }

  // =====================================================================================================
  // 4. Future city (optional)
  // =====================================================================================================
  if (E.future) {
    const F = E.future, FS = F.summary, USE = { residential: "residential", mixed: "mixed use", commercial: "commercial / industrial", special: "special / public" };
    const sf = section("future", "Future city: does the plan survive new districts?",
      `Heilbronn's land-use plan and the regional plan mark land for future building. Where that land is still farmland today (ALKIS), we add buildings at the
       zone's typical density and a worst-case height (90th percentile of nearby buildings of the same use), each with one new meter, and re-check every radio path.`);
    sf.insertAdjacentHTML("beforeend", `<div class="kpi-wall k3">
      ${tile("Unbuilt building land", `${n0(FS.open_ha)} ha`, `${n0(FS.open_areas)} areas, of ${n0(FS.zone_ha)} ha designated for building`)}
      ${tile("Planned buildings = new meters", n0(FS.future_buildings), `${Object.entries(FS.heights || {}).map(([u, h]) => `${USE[u] || u} ≈ ${n0(h)} m`).join(" · ")}`)}
      ${FUT ? tile("Today's 98 % plan in the future city", pc(FUT.today_plan_future), "falls below the target", "warn") : ""}
    </div>`);
    sf.appendChild(card("Sites needed: today vs future city", "The future plan keeps the target in both situations (hover a row for details).",
      dumbbell(F.plans, { name: (r) => `${Math.round(r.target * 100)} % target`, a: (r) => r.k_today, b: (r) => r.k_future, aName: "today", bName: "future city",
        tip: (r) => tipHtml(`${r.k_today} → ${r.k_future} sites`, `${Math.round(r.target * 100)} % target`, [
          ["Today's plan in the future city", pc(r.today_plan_future)], ["Future plan today", pc(r.future_plan_today)],
          ["Sites added", esc((r.extra_sites || []).join(", ") || "–")], ["Sites dropped", esc((r.dropped_sites || []).join(", ") || "–")]]),
        aria: "Sites needed today versus in the future city" })));
    sf.appendChild(table({
      title: "Today's plan vs the future-proof plan", file: "future-city-plans", sort: [0, 1],
      cols: [
        { k: "target", label: "Target", num: true, fmt: (v) => pc(v, 0) },
        { k: "k_today", label: "Sites (today)", num: true },
        { k: "k_future", label: "Sites (future city)", num: true, fmt: (v) => `<b>${v}</b>` },
        { k: "today_plan_future", label: "Today's plan: future", num: true, fmt: (v, r) => `<span class="${v < r.target ? "neg" : ""}">${pc(v, 2)}</span>` },
        { k: "future_plan_today", label: "Future plan: today", num: true, fmt: (v) => pc(v, 2) },
        { k: "extra_sites", label: "Sites added", fmt: (v) => esc((v || []).join(", ") || "–"), csv: (v) => (v || []).join(" ") },
        { k: "dropped_sites", label: "Sites dropped", fmt: (v) => esc((v || []).join(", ") || "–"), csv: (v) => (v || []).join(" ") },
      ],
      foot: "Red = today's plan falls below the target once the planned districts exist.",
      rows: F.plans,
    }));
    sf.appendChild(table({
      title: "Unbuilt building land", file: "future-building-land", search: true, maxH: 420, sort: [4, -1],
      cols: [
        { k: "id", label: "#", num: true },
        { k: "kind", label: "Source", fmt: (v, r) => `${esc(v)}${r.plan ? `<br><span class="quote">${esc(r.plan)}</span>` : ""}` },
        { k: "use", label: "Use", fmt: (v) => esc(USE[v] || v) },
        { k: "height_m", label: "Worst-case height m", num: true, fmt: n1 },
        { k: "ha", label: "Area ha", num: true, fmt: n1 },
        { k: "buildings", label: "Planned buildings", num: true, fmt: n0 },
        { k: "lat", label: "Location", fmt: (v, r) => `${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}`, csv: (v, r) => `${r.lat} ${r.lon}` },
      ],
      foot: "Plans say what may be built, not when or how high. Heilbronn's land-use plan has been in force since 2003.",
      rows: F.areas,
    }));
  }

  // =====================================================================================================
  // 5. Model
  // =====================================================================================================
  const s4 = section("model", "Model: can the predictions be trusted?",
    `Tested the honest way: <b>new area</b> = whole 1 km blocks held out; <b>new gateway</b> = each gateway left out in turn and predicted from the others –
     exactly the planning question "how would a site we never measured perform?". Models are chosen by new-gateway log-loss, because the planner uses the
     probabilities themselves.`);
  const NAMES = { catboost: "CatBoost", gbm: "Gradient boosting", mlp: "Neural network", physics: "Physics baseline" };
  const bench = (E.benchmark || []).filter((r) => isNum(r.logo_auc)).sort((a, b) => b.logo_auc - a.logo_auc);
  const LABEL = { log_d: "distance (log)", d_m: "distance", dz_m: "height difference gateway–meter", terr_obs_max_m: "terrain in the way",
    rx_ground_z: "ground elevation at meter", rx_bdens: "building density at meter", rx_bldg_h: "building height at meter",
    obs_max_m: "highest obstacle on the path", obs_frac: "share of path blocked", fresnel_frac: "radio zone (Fresnel) blocked",
    bldg_cross: "buildings crossed", bldg_len_m: "metres through buildings", forest_len_m: "metres through forest",
    tx_h_agl: "gateway antenna height", elev_angle_deg: "elevation angle", fspl_db: "free-space loss", terr_obs_frac: "share of path below terrain",
    valid_frac: "3D data coverage of path", is_apa: "meter maker: Apator", is_axi: "meter maker: Axioma" };
  const imp = (E.importance || []).slice(0, 10).map(([f, v]) => ({ f, name: LABEL[f] || f, v }));
  const dlo = bench.length ? Math.floor(Math.min(...bench.map((r) => r.logo_auc)) * 50) / 50 : 0.9;
  const benchCard = bench.length ? card(`${bench.length} learners on unseen gateways`, "AUC when each gateway is left out (higher = better). Note the axis starts at " + dlo.toFixed(2) + ".",
    dotplot(bench, { name: (r) => r.model, value: (r) => r.logo_auc, fmt: n3, hl: (r) => r.model === O.model, domain: [dlo, 1],
      ticks: [dlo, (dlo + 1) / 2, 1].map((v) => Math.round(v * 1000) / 1000), tickFmt: (v) => v.toFixed(2),
      tip: (r) => tipHtml(`AUC ${n3(r.logo_auc)}`, esc(r.model), [["Log-loss (new gateway)", n3(r.logo_logloss)], ["AUC (new area)", n3(r.spatial_auc)],
        ["vs previous model", esc(r.vs_gbm_ci || "reference")]]), aria: "Learner comparison" })) : null;
  const impCard = card("What drives reception", "How much the ranking quality drops when one factor is scrambled (top 10).",
    hbars(imp, { name: (r) => r.name, value: (r) => r.v, fmt: (v) => v.toFixed(4) }));
  s4.appendChild(benchCard ? row(benchCard, impCard) : impCard);
  s4.insertAdjacentHTML("beforeend", `<div class="kpi-wall k4">
    ${tile("Selected model", esc(NAMES[O.model] || O.model), "lowest new-gateway log-loss")}
    ${tile("AUC on unseen gateways", n3(O.logo_auc), "1 = perfect ranking")}
    ${tile("Log-loss on unseen gateways", n3(O.logo_logloss), "lower = better calibrated")}
    ${tile("Signal-strength error", `${n1(O.rssi_mae)} dB`, `textbook formula: ${n1(O.rssi_mae_textbook)} dB`)}</div>`);
  const byModel = {};
  (E.models || []).forEach((r) => { (byModel[r.model] ||= { model: r.model })[r.scheme] = r; });
  s4.appendChild(table({
    title: "Validation of the main models", file: "model-validation", sort: [4, 1], rowClass: (r) => (r.model === O.model ? "hl" : ""),
    cols: [
      { k: "name", label: "Model", fmt: (v) => `<b>${esc(v)}</b>` },
      { k: "sp_auc", label: "New area AUC", num: true, fmt: n3 }, { k: "sp_ll", label: "New area log-loss", num: true, fmt: n3 },
      { k: "lo_auc", label: "New gateway AUC", num: true, fmt: n3 }, { k: "lo_ll", label: "New gateway log-loss", num: true, fmt: n3 },
      { k: "lo_brier", label: "Brier", num: true, fmt: n3 },
      { k: "dev_pred", label: "Meters heard: predicted", num: true, fmt: (v) => pc(v, 0) }, { k: "dev_true", label: "… measured", num: true, fmt: (v) => pc(v, 0) },
    ],
    foot: "AUC: 1 = perfect ranking, 0.5 = coin flip. Log-loss and Brier: lower is better – they reward well-calibrated probabilities, which the planner needs.",
    rows: Object.values(byModel).map((m) => ({ model: m.model, name: NAMES[m.model] || m.model, sp_auc: m.spatial?.auc, sp_ll: m.spatial?.logloss,
      lo_auc: m.logo?.auc, lo_ll: m.logo?.logloss, lo_brier: m.logo?.brier, dev_pred: m.device?.mean_pred, dev_true: m.device?.mean_true })),
  }));
  if (bench.length) s4.appendChild(table({
    title: `Benchmark of ${bench.length} learners`, file: "model-benchmark", sort: [2, 1], rowClass: (r) => (r.model === O.model ? "hl" : ""),
    cols: [
      { k: "model", label: "Learner", fmt: (v) => `<b>${esc(v)}</b>` },
      { k: "logo_auc", label: "New gateway AUC", num: true, fmt: n3 }, { k: "logo_logloss", label: "New gateway log-loss", num: true, fmt: n3 },
      { k: "logo_brier", label: "Brier", num: true, fmt: n3 }, { k: "logo_bacc", label: "Balanced accuracy", num: true, fmt: (v) => pc(v) },
      { k: "spatial_auc", label: "New area AUC", num: true, fmt: n3 },
      { k: "vs_gbm_ci", label: "Log-loss vs previous model (95 % CI)", fmt: (v) => esc(v || "reference") },
    ],
    foot: "Plain accuracy is misleading here: 84 % of links are 'not heard', so always guessing 'no' already scores 84 %. CI from a paired bootstrap over meters.",
    rows: bench,
  }));

  // =====================================================================================================
  // 6. Measurements – facts from the radio data alone (no model)
  // =====================================================================================================
  const s5 = section("measured", "Measurements: what the real week shows",
    `Facts from HNVG's reception data alone – no model involved. Every eligible meter × every existing gateway is one link; "heard" = the gateway
     received at least one message from that meter in the week ${esc(week)}.`);
  const RBD = E.reception_by_distance || [];
  const distCard = card("Reception by distance", "Share of meter–gateway links heard. HNVG's 300 m rule sits inside the first band.",
    hbars(RBD, { name: (r) => r.band, value: (r) => r.heard, fmt: (v) => pc(v, 0), max: 1,
      tip: (r) => tipHtml(pc(r.heard, 0), `heard at ${esc(r.band)}`, [["Links", n0(r.links)], ["Median signal", `${n0(r.rssi_med)} dBm`], ["Median SNR", `${n1(r.snr_med)} dB`]]) }));
  const bm = E.by_maker || {}, BANDS = [["(0, 500]", "≤ 500 m"], ["(500, 1000]", "500–1000 m"], ["(1000, 2000]", "1–2 km"]];
  const SER = [["IMT", "#3987e5"], ["APA", "#d95926"], ["AXI", "#199e70"]].filter(([m]) => bm[m])
    .map(([m, color]) => ({ key: m, color, name: `${MAKER[m]} (${bm[m].meters} meters)` }));
  const makerCard = card("Reception by meter maker", "Same distances, different makers – Apator is heard clearly less often (Axioma: only 16 meters, small sample).",
    ghbars(BANDS.map(([k, label]) => ({ k, label })), SER, { value: (g, s) => bm[s.key].heard_by_distance[g.k], fmt: (v) => pc(v, 0), max: 1,
      rows: (g, s) => [["Meters of this maker", n0(bm[s.key].meters)]] }));
  s5.appendChild(row(distCard, makerCard));
  const st = Object.fromEntries((E.meter_status || []).map((r) => [r.meter_state, r]));
  const dr = E.delivery_ratio || {}, RO = E.radio_observed || {};
  s5.insertAdjacentHTML("beforeend", `<div class="kpi-wall k4">
    ${st.A ? tile("Active meters (A) heard", pc(st.A.heard_share, 0), `${n0(st.A.meters)} meters – only ${n0(st.A.meters * (1 - st.A.heard_share))} silent: the real coverage holes`) : ""}
    ${st.I ? tile("Inactive meters (I) heard", pc(st.I.heard_share, 0), `${n0(st.I.meters)} meters – switched off, not dead zones`) : ""}
    ${tile("Messages delivered (median meter)", pc(dr.median, 0), `${pc(dr.share_below_80, 0)} of meters lose more than 20 %`)}
    ${tile("Weakest signals received", `${n0(RO.rssi_floor_dbm)} dBm`, "urban noise limit – datasheet: −141 dBm")}</div>`);
  const load = (E.gateway_load || []).slice().sort((a, b) => b.receptions - a.receptions);
  const loadCard = card("Load per existing gateway", "Receptions in the week – two gateways in the west carry most of the traffic.",
    hbars(load, { name: (r) => String(r.name || r.gateway_id).replace(/^Heilbronn[-\s]+/, ""), value: (r) => r.receptions, fmt: n0,
      tip: (r) => tipHtml(`${n0(r.receptions)} receptions`, esc(r.name || r.gateway_id), [["Meters", n0(r.devices)], ["Share", pc(r.share)]]) }));
  const hist = (dr.hist || []).map((n, i) => ({ bin: `${i * 10}–${i * 10 + 10} %`, n, i }));
  const histCard = card("Message delivery per meter", "How many meters deliver which share of their messages (from frame counters).",
    columns(hist, { value: (r) => r.n, name: (r) => `${r.n} meters deliver ${r.bin}`, fmt: n0, axFmt: n0, tick: (r) => `${r.i * 10}`, xLabel: "% of messages delivered",
      labelIf: (r) => r.i === hist.length - 1, aria: "Delivery ratio histogram" }));
  s5.appendChild(row(loadCard, histCard));
  const G = E.hardware?.gateway || {}, LB = E.link_budget || {}, SPEC = { 7: -7.5, 8: -10, 9: -12.5, 10: -15, 11: -17.5, 12: -20 };
  s5.appendChild(table({ title: "Hardware: datasheet vs measured", file: "hardware-check",
    cols: [{ k: "item", label: "Item", fmt: (v) => `<b>${esc(v)}</b>` }, { k: "spec", label: "Datasheet (public)" }, { k: "seen", label: "Measured in the data" }],
    rows: [
      { item: "Gateway", spec: G.model || "–", seen: `${O.existing} gateways running` },
      { item: "Receiver sensitivity", spec: `${G.rx_sensitivity_dbm_sf12 ?? LB.gateway_sensitivity_dbm} dBm (SF12)`, seen: `weakest signals ≈ ${n0(RO.rssi_floor_dbm)} dBm – urban noise floor` },
      { item: "Antenna / mounting", spec: `${G.antenna || "–"}; ${G.mounting || "–"}`, seen: `gain assumed ${LB.gateway_antenna_dbi} dBi; tallest roof + 2 m` },
      { item: "Meter transmit power", spec: `${LB.device_tx_dbm} dBm (25 mW)`, seen: "–" },
      ...Object.entries(RO.snr_min_by_sf || {}).map(([sf, v]) => ({ item: `Lowest SNR at SF${sf}`, spec: `LoRa limit ${SPEC[sf]} dB`, seen: `${n1(v)} dB` })),
      { item: "Messages per meter and day", spec: "configurable", seen: n1(RO.msgs_per_meter_day) },
    ] }));
  const SFrows = E.sf_mix || [];
  if (SFrows.length) s5.appendChild(table({ title: "Spreading factors (radio settings)", file: "spreading-factors", sort: [0, 1],
    cols: [{ k: "sf", label: "SF", num: true, fmt: (v) => `SF${v}` }, { k: "n", label: "Messages", num: true, fmt: n0 },
      { k: "share", label: "Share", num: true, fmt: (v) => pc(v) }, { k: "toa_ms", label: "Airtime per message", num: true, fmt: (v) => `${n0(v)} ms` },
      { k: "airtime_share", label: "Share of airtime", num: true, fmt: (v) => pc(v) }],
    foot: "Weak links use slower settings (SF11/12): they reach further but cost airtime and battery.", rows: SFrows }));

  // =====================================================================================================
  // 7. Weather
  // =====================================================================================================
  const WX = E.weather;
  if (WX && WX.rain) {
    const wk = WX.week || {}, yr = WX.year || {};
    const s5b = section("weather", "Weather: does it change reception?",
      `Every received message is compared with the normal level of its own meter–gateway link, hour by hour, against Open-Meteo weather.
       The lines are 95 % intervals: if a line crosses 0, no effect could be measured.`);
    const any = [WX.rain.rssi_db, WX.rain.snr_db, WX.trend?.rssi_db_per_10C, WX.trend?.rssi_db_per_10pct_rh].some((x) => x && (x.lo > 0 || x.hi < 0));
    s5b.insertAdjacentHTML("beforeend", `<div class="verdict ${any ? "warn" : "ok"}"><span class="vd-icon" aria-hidden="true">${any ? "!" : "✓"}</span>
      <div><b>${any ? "A weather effect was measured" : "No measurable weather effect in the measured week"}</b><p>${esc(WX.verdict || "")}</p></div></div>`);
    const fr = [
      ["Signal in rain vs dry", WX.rain.rssi_db], ["SNR in rain vs dry", WX.rain.snr_db], ["Signal per +10 °C", WX.trend?.rssi_db_per_10C],
      ["Signal per +10 % humidity", WX.trend?.rssi_db_per_10pct_rh], ["SNR per +10 % humidity", WX.trend?.snr_db_per_10pct_rh],
    ].filter(([, x]) => x && isNum(x.value)).map(([label, x]) => ({ label, ...x }));
    s5b.appendChild(card("Effect on the radio signal (dB)", `Based on ${n0(wk.receptions)} receptions in ${n0(wk.hours)} hours, ${n0(wk.rain_hours)} of them rainy. Grey = no measurable effect.`,
      forest(fr, { unit: "dB", aria: "Weather effects with 95 % intervals" })));
    s5b.appendChild(row(
      card("Messages delivered", "Share of sent messages that arrived.", hbars([
        { name: "Rainy hours", v: WX.rain.delivery_wet }, { name: "Dry hours", v: WX.rain.delivery_dry }],
        { name: (r) => r.name, value: (r) => r.v?.value, fmt: (v) => pc(v, 0), max: 1,
          tip: (r) => tipHtml(pc(r.v?.value, 0), esc(r.name), [["95 % interval", `${pc(r.v?.lo, 0)} … ${pc(r.v?.hi, 0)}`]]) })),
      card("How typical was the week?", "What the week did and did not contain.", `<div class="cmp">
        <div class="cmp-h"><span></span><span>Measured week</span><span>Whole year</span></div>
        <div><span>Temperature</span><b>${n1(wk.temp_min)} … ${n1(wk.temp_max)} °C</b><b>${n1(yr.temp_min)} … ${n1(yr.temp_max)} °C</b></div>
        <div><span>Rainy hours</span><b>${n0(wk.rain_hours)}</b><b>${n0(yr.rain_hours_per_week_median)} per week</b></div>
        <div><span>Heaviest rain</span><b>${n1(yr.week_max_rain_mm_h)} mm/h</b><b>${n1(yr.max_rain_mm_h)} mm/h</b></div>
        <div><span>Frost / snow hours</span><b>${wk.temp_min < 0 ? "yes" : "0 / 0"}</b><b>${n0(yr.frost_hours)} / ${n0(yr.snow_hours)}</b></div>
        <div class="cmp-f"><span>Year's temperature range covered</span><b>${pc(yr.share_of_year_temp_range_covered, 0)}</b><b>100 %</b></div></div>
        <p class="cc-note">Snow, frost, summer heat and leaves on trees are untested – a full year of HNVG radio data would confirm the plan for all seasons.</p>`)));
  }

  // =====================================================================================================
  // 8. Meters
  // =====================================================================================================
  const heard = M.filter((m) => m.rssi != null).length;
  const s6 = section("meters", `Measured water meters (${M.length})`,
    `Every meter with coordinates in HNVG's list and what the week of radio data says about it: ${n0(heard)} heard, ${n0(M.length - heard)} not heard
     (mostly inactive meters).`);
  const statuses = [...new Set(M.map((m) => m.st))].sort();
  s6.appendChild(table({
    title: "All meters", file: "meters", search: true, maxH: 600, sort: [4, -1],
    filter: { options: [["all", "All meters"], ["heard", "Heard this week"], ["silent", "Not heard"], ...["APA", "IMT", "AXI"].map((m) => [m, `Maker: ${MAKER[m]}`]), ...statuses.map((s) => [`st:${s}`, `Status ${s}`])],
      test: (r, f) => f === "all" || (f === "heard" && r.rssi != null) || (f === "silent" && r.rssi == null) || r.mk === f || f === `st:${r.st}` },
    cols: [
      { k: "adr", label: "Address", fmt: (v) => esc(v || "–") },
      { k: "mk", label: "Maker", fmt: (v) => esc(MAKER[v] || v) },
      { k: "st", label: "Status" },
      { k: "heard", label: "Heard", sort: (r) => (r.rssi != null ? 1 : 0), fmt: (v, r) => (r.rssi != null ? pill("yes", "yes") : pill("no", "no")), csv: (v, r) => (r.rssi != null ? "yes" : "no") },
      { k: "rssi", label: "Best RSSI", num: true, fmt: (v) => (v == null ? "–" : `${n0(v)} dBm`) },
      { k: "gw", label: "Best gateway", fmt: (v) => esc(v || "–") },
      { k: "n_gw", label: "Gateways", num: true, fmt: n0 },
      { k: "pdr", label: "Delivery", num: true, fmt: (v) => pc(v, 0) },
      { k: "src", label: "Location", fmt: (v) => esc(v === "provided" ? "as provided" : v ? "repaired from address" : "–") },
      { k: "yr", label: "Calibrated", num: true, fmt: (v) => esc(v ?? "–") },
      { k: "sz", label: "Size", fmt: (v) => esc(v ?? "–") },
    ],
    foot: "Status: A = active, I = inactive, U = unknown (HNVG). Addresses are HNVG data – ask HNVG before any public use.",
    rows: M,
  }));

  // =====================================================================================================
  // 9. Sources & definitions
  // =====================================================================================================
  const s7 = section("sources", "Sources and definitions", "Where every input comes from and how the key terms are defined – for questions in the Q&A.");
  const DS = {
    transmissions: ["Radio receptions, 1 week", "HNVG", "labels heard / not heard, RSSI, SNR"],
    transmitter_meta: ["Water meter list", "HNVG", "meter position, status, maker, size"],
    mapping: ["Meter ↔ radio device mapping", "HNVG", "joins meters to receptions"],
    gateway_meta: ["Existing gateways", "HNVG", "positions of the 8 gateways"],
    candidates: ["Approved candidate sites", "HNVG", "71 possible new gateway positions"],
    addresses: ["Heilbronn address list", "HNVG", "what the plan has to cover"],
    alkis_buildings: ["ALKIS building outlines", "LGL Open GeoData", "addresses → coordinates"],
    alkis_landuse: ["ALKIS land use", "LGL Open GeoData", "forest, roads, unbuilt land"],
    lod_gml: ["3D buildings LoD1/LoD2", "LGL Open GeoData", "building heights, radio obstacles, gateway roofs"],
    dom1: ["Surface model DOM1 (1 m)", "LGL Open GeoData", "line of sight incl. trees and structures"],
    dgm1: ["Terrain model DGM1 (1 m)", "LGL Open GeoData", "ground height, terrain in the way"],
    dop20: ["Orthophotos DOP20 RGBI", "LGL Open GeoData", "tree canopy, aerial imagery"],
    house_coords: ["Hauskoordinaten", "LGL Open GeoData", "official address points"],
    dlm: ["Basis-DLM (forest, water)", "LGL Open GeoData", "forest and water beyond the city"],
    tab_wasser: ["TAB Wasser", "HNVG website", "meter at the house entry"],
    water_conditions: ["Ergänzende Bedingungen AVBWasserV", "HNVG website", "meter shaft rule (> 40 m)"],
    water_service_prices: ["Montagepreise Wasserversorgung", "HNVG website", "cost of a manual reading"],
    water_tariff: ["Preisblatt AVBWasserV", "HNVG website", "water price"],
    weather: ["Weather, hourly", "Open-Meteo", "rain, temperature, humidity, snow vs reception"],
    xplan: ["Spatial plans (XPlanung)", "Geoportal Raumordnung BW", "future city: land-use and regional plans"],
  };
  const mc = E.sources?.manifest_counts || {};
  s7.appendChild(row(
    card("Key terms", "", `<dl class="facts">
      <dt>Coverage</dt><dd>Expected share of addresses whose basement meter is heard by at least one gateway: 1 − Π(1 − p) over the active gateways, weighted by addresses.</dd>
      <dt>Within 300 m</dt><dd>HNVG's conservative rule of thumb; shown for reference – measured reception reaches further.</dd>
      <dt>Fewest sites for a target</dt><dd>Greedy build order → shortest prefix reaching the target → pruning and 2-for-1 swaps; no site of the result can be removed.</dd>
      <dt>New area / new gateway</dt><dd>Validation where whole 1 km blocks, or each gateway, are left out – the model is always scored on data it never saw.</dd>
      <dt>Gateway height</dt><dd>Mounting points are unknown: tallest building roof within 30 m + 2 m; else a tower/structure; else a 10 m mast. Trees are never used.</dd></dl>`),
    card("Data at a glance", "", `<div class="srcs">${["HNVG", "LGL Open GeoData", "HNVG website", "Open-Meteo", "Geoportal Raumordnung BW"].map((src) => {
      const ks = Object.entries(DS).filter(([k, v]) => v[1] === src && (mc[k] || 0) > 0);
      return ks.length ? `<div class="src"><b>${esc(src)}</b><span>${ks.map(([, v]) => esc(v[0])).join(" · ")}</span></div>` : "";
    }).join("")}</div><p class="cc-note">Generated ${esc(E.generated || "–")}. Terrain: ${esc(E.sources?.terrain || "–")}.</p>`)));
  s7.appendChild(table({ title: "Input data in detail", file: "data-sources",
    cols: [{ k: "name", label: "Dataset", fmt: (v) => `<b>${esc(v)}</b>` }, { k: "src", label: "Source" }, { k: "files", label: "Files used", num: true, fmt: n0 }, { k: "use", label: "Used for" }],
    rows: Object.entries(DS).map(([k, [name, src, use]]) => ({ name, src, use, files: mc[k] ?? 0 })),
    foot: "Zips are read in place; files that do not match a dataset pattern are ignored." }));
  if (E.hnvg_docs) {
    s7.appendChild(table({ title: "Facts read from HNVG's documents", file: "hnvg-facts",
      cols: [{ k: "key", label: "Fact", fmt: (v, r) => `<b>${esc(r.meaning || v)}</b>` }, { k: "value", label: "Value", fmt: (v, r) => `${esc(v)} ${esc(r.unit || "")}` },
        { k: "source", label: "Document" }, { k: "found_in_document", label: "Found", fmt: (v) => (v ? pill("yes", "yes") : pill("default", "medium")) },
        { k: "evidence", label: "Text in the document", fmt: (v) => `<span class="quote">${esc(v || "")}</span>` }],
      rows: Object.entries(E.hnvg_docs).map(([key, f]) => ({ key, ...f })) }));
  }

  finishNav();
  function finishNav() {
    const links = [...document.querySelectorAll("#nav a")];
    const obs = new IntersectionObserver((es) => es.forEach((e) => {
      if (e.isIntersecting) links.forEach((a) => a.classList.toggle("on", a.getAttribute("href") === `#${e.target.id}`));
    }), { rootMargin: "-40% 0px -55% 0px" });
    document.querySelectorAll(".doc-section").forEach((s) => obs.observe(s));
    if (location.hash) document.querySelector(location.hash)?.scrollIntoView();
  }
})();
