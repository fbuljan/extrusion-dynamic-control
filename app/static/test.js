// Test bench: the width loop over the last minutes, and everything needed to run a test by hand (zone, Auto, lever
// tuning, disturbances). Rows come from the server's 1 Hz trace, so every page shows the same history.
const LEVERS = [
  { op: "flow_percent", name: "Flow", unit: "%", min: 0, max: 200, key: "flow", widens: 1 },
  { op: "speed_override_percent", name: "Speed", unit: "%", min: 10, max: 200, key: "speed", widens: -1 },
  { op: "nozzle_temperature_c", name: "Nozzle", unit: "°C", min: 190, max: 250, key: "nozzle_target", widens: 1 },
];
const BY_OP = Object.fromEntries(LEVERS.map(l => [l.op, l]));
const NUDGES = [-10, -5, -1, 1, 5, 10];
const KEEP_MS = 15 * 60_000;
const ESTIMATE_STALE_MS = 5000, ROW_STALE_MS = 4000, LINK_STALE_MS = 5000, MSG_MS = 8000;
const MATCH_MS = 20_000;  // a readback change this soon after our own send of the same value is that send landing
const WALLS = /^alarm:\s*below ([\d.]+) mm \(walls may not touch\);?\s*/;
const isNum = v => typeof v === "number" && Number.isFinite(v);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const round3 = v => Math.round(v * 1000) / 1000;
const mmText = v => (Math.abs(v * 100 - Math.round(v * 100)) < 1e-6 ? v.toFixed(2) : v.toFixed(3));
const node = (tag, className = "", text = "") => Object.assign(document.createElement(tag), { className, textContent: text });
const signed = (v, d = 2) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}`;
const clock = t => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const unitOf = op => BY_OP[op]?.unit ?? "";
const nameOf = op => BY_OP[op]?.name.toLowerCase() ?? op;
const moveText = (op, from, to) => `${nameOf(op)} ${isNum(from) ? `${fmt(from, 0)} → ` : "→ "}${fmt(to, 0)} ${unitOf(op)}`;

// ---- state ------------------------------------------------------------------------------------------------------------
let rows = [], serverEvents = new Map(), derived = [], lastRowT = null, serverOffset = 0;
let frames = [], frameKeys = new Set();  // every IR frame's outcome: {t_ms, mm, reason, group}
let traceOkAt = 0, traceFailed = false, control = null, controlOkAt = 0, controlFailed = false;
let estimate = null;  // {mm, sigma_mm, confidence, n, at}: `at` in browser time
let windowMin = [5, 10, 15].includes(prefs.get("bench.window")) ? prefs.get("bench.window") : 10;
const serverNow = () => Date.now() + serverOffset;
const linkOk = () => !traceFailed && Date.now() - traceOkAt <= LINK_STALE_MS;
const controlOk = () => !controlFailed && control && Date.now() - controlOkAt <= LINK_STALE_MS;
const lastRow = () => rows[rows.length - 1] || null;
const liveRow = () => { const r = lastRow(); return r && linkOk() && serverNow() - r.t_ms <= ROW_STALE_MS ? r : null; };
const liveEstimate = () => (estimate && Date.now() - estimate.at <= ESTIMATE_STALE_MS ? estimate : null);
const wallsMm = () => control?.settings?.min_width_mm ?? control?.limits?.min_width_mm;
const bandOf = c => {
  const d = isNum(c?.limits?.band_default_mm) ? c.limits.band_default_mm : 0.05;
  return [isNum(c?.band_below_mm) ? c.band_below_mm : d, isNum(c?.band_above_mm) ? c.band_above_mm : d];
};

// ---- layout: side panel width, remembered per browser -------------------------------------------------------------------
const bench = $("#bench");
const RAIL_MIN = 340, RAIL_MAX = 620, RAIL_DEFAULT = 410;
let railWidth = clamp(prefs.get("bench.rail", RAIL_DEFAULT), RAIL_MIN, RAIL_MAX);
const applyRail = () => { bench.style.setProperty("--rail-w", `${railWidth}px`); prefs.set("bench.rail", railWidth); };
draggable($("#rail-splitter"), {
  onDrag: e => { railWidth = clamp(bench.getBoundingClientRect().right - e.clientX, RAIL_MIN, RAIL_MAX); applyRail(); },
  onKey: step => { railWidth = clamp(railWidth - step * 16, RAIL_MIN, RAIL_MAX); applyRail(); },
  onReset: () => { railWidth = RAIL_DEFAULT; applyRail(); },
});
applyRail();

// ---- IR thumbnail and frame rate (proof the camera feed is alive) -------------------------------------------------------
const showIr = cameraView("#ir", "#ir-label", "#ir-empty", "IR");
const frameTimes = [], pageStart = Date.now();
on("frame", f => {
  if (f.camera_kind !== "ir") return;
  showIr(f);
  frameTimes.push(Date.now());
});
on("snapshot", m => { if (m.frames?.ir) showIr(m.frames.ir); });
function renderVitals() {
  while (frameTimes.length && Date.now() - frameTimes[0] > 5000) frameTimes.shift();
  const span = Math.min(5, (Date.now() - pageStart) / 1000), fps = frameTimes.length / Math.max(span, 1);
  if (!frameTimes.length && span < 5) led($("#st-ir"), "IR waiting", "off");
  else led($("#st-ir"), fps > 0 ? `IR ${fps.toFixed(1)} fps` : "IR no frames", fps >= 1 ? "ok" : fps > 0 ? "warn" : "bad");
  const r = lastRow(), age = r ? (serverNow() - r.t_ms) / 1000 : null;
  led($("#st-loop"), !linkOk() ? "loop offline" : age === null ? "loop waiting" : `loop ${age < 1.5 ? "live" : `${age.toFixed(0)} s ago`}`,
    !linkOk() ? "bad" : age !== null && age < 3 ? "ok" : "warn");
}

// ---- charts -------------------------------------------------------------------------------------------------------------
const css = getComputedStyle(document.documentElement);
const tok = name => css.getPropertyValue(name).trim();
const C = { accent: tok("--accent"), ok: tok("--ok"), warn: tok("--warn"), bad: tok("--bad"), pending: tok("--pending"),
  silk: tok("--silk"), muted: tok("--muted"), line: tok("--line-soft"), text: tok("--text") };
const tint = (color, pct) => `color-mix(in oklch, ${color} ${pct}%, transparent)`;
Chart.defaults.color = C.muted;
Chart.defaults.borderColor = C.line;
Chart.defaults.font.family = tok("--mono");
Chart.defaults.font.size = 11;

const MARK = { auto: C.accent, manual: C.pending, outside: C.warn };
// Auto-on spans tint the plot; commands are vertical lines (labelled on the width plot only).
const benchPlugin = {
  id: "bench",
  beforeDatasetsDraw(chart, _, o) {
    const { ctx, chartArea: a, scales: { x } } = chart;
    ctx.save();
    for (const [t0, t1, dry] of o.lanes || []) {
      const x0 = clamp(x.getPixelForValue(t0), a.left, a.right), x1 = clamp(x.getPixelForValue(t1), a.left, a.right);
      if (x1 - x0 < 0.5) continue;
      ctx.fillStyle = tint(dry ? C.warn : C.accent, 6);
      ctx.fillRect(x0, a.top, x1 - x0, a.bottom - a.top);
      if (o.labels) { ctx.fillStyle = dry ? C.warn : C.accent; ctx.fillRect(x0, a.bottom - 3, x1 - x0, 3); }
    }
    ctx.restore();
  },
  afterDatasetsDraw(chart, _, o) {
    const { ctx, chartArea: a, scales: { x, y } } = chart;
    ctx.save();
    if (o.dots?.length) {  // one dot per measured frame: what the average line is made of
      ctx.fillStyle = tint(C.accent, 55);
      for (const d of o.dots) {
        const px = x.getPixelForValue(d.t_ms), py = y.getPixelForValue(d.mm);
        if (px < a.left || px > a.right || py < a.top || py > a.bottom) continue;
        ctx.beginPath(); ctx.arc(px, py, 2.1, 0, 2 * Math.PI); ctx.fill();
      }
    }
    ctx.font = `600 10.5px ${tok("--mono")}`;
    const rowsRight = [-Infinity, -Infinity, -Infinity];
    for (const m of o.marks || []) {
      const px = x.getPixelForValue(m.t);
      if (px < a.left || px > a.right) continue;
      ctx.strokeStyle = MARK[m.kind];
      ctx.globalAlpha = o.labels ? 0.9 : 0.55;
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(px + 0.5, a.top); ctx.lineTo(px + 0.5, a.bottom); ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      if (!o.labels) continue;
      const w = ctx.measureText(m.label).width + 10, left = Math.min(px + 3, a.right - w);
      const row = rowsRight.findIndex(r => r < left - 4);
      if (row < 0) continue;  // three rows full here: the event list still has it
      rowsRight[row] = left + w;
      const y = a.top + 4 + row * 17;
      ctx.fillStyle = tok("--panel");
      ctx.fillRect(left, y, w, 15);
      ctx.strokeStyle = MARK[m.kind];
      ctx.strokeRect(left + 0.5, y + 0.5, w - 1, 14);
      ctx.fillStyle = MARK[m.kind];
      ctx.fillText(m.label, left + 5, y + 11);
    }
    ctx.restore();
  },
};

const minuteTicks = axis => {
  const step = windowMin > 10 ? 120_000 : 60_000, out = [];
  for (let v = Math.ceil(axis.min / step) * step; v <= axis.max; v += step) out.push({ value: v });
  axis.ticks = out;
};
function timeScale(showTicks) {
  return { type: "linear", afterBuildTicks: minuteTicks, grid: { color: C.line, drawTicks: false },
    ticks: { display: showTicks, maxRotation: 0, padding: 6,
      callback: v => new Date(v).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) } };
}
const yWidth = axis => { axis.width = 54; };
const line = (label, color, extra = {}) => ({ label, data: [], borderColor: color, backgroundColor: color, borderWidth: 1.5,
  pointRadius: 0, pointHoverRadius: 3, spanGaps: false, ...extra });

function makeChart(id, datasets, { ticks = false, main = false, y = {} } = {}) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { datasets },
    plugins: [benchPlugin],
    options: {
      animation: false, maintainAspectRatio: false, parsing: false, normalized: true,
      layout: { padding: { right: 8, top: main ? 4 : 2 } },
      interaction: { mode: "index", intersect: false },
      scales: { x: timeScale(ticks), y: { afterFit: yWidth, grid: { color: C.line }, ticks: { maxTicksLimit: main ? 7 : 3 }, ...y } },
      plugins: {
        legend: { display: false },
        bench: { lanes: [], marks: [], labels: main },
        tooltip: {
          backgroundColor: tok("--panel-hi"), borderColor: tok("--line"), borderWidth: 1, titleColor: C.text, bodyColor: C.silk,
          titleFont: { family: tok("--mono"), weight: 600 }, bodyFont: { family: tok("--mono") }, padding: 9, boxPadding: 4,
          filter: item => !item.dataset.quiet && isNum(item.parsed.y),
          callbacks: { title: items => (items.length ? clock(items[0].parsed.x) : "") },
        },
      },
    },
  });
}

const widthChart = makeChart("c-width", [
  line("zone max", tint(C.ok, 55), { borderWidth: 1, stepped: "before", fill: { target: 1 }, backgroundColor: tint(C.ok, 11), quiet: true }),
  line("zone min", tint(C.ok, 55), { borderWidth: 1, stepped: "before", quiet: true }),
  line("noise high", "transparent", { borderWidth: 0, fill: { target: 3 }, backgroundColor: tint(C.accent, 16), quiet: true }),
  line("noise low", "transparent", { borderWidth: 0, quiet: true }),
  line("target", C.silk, { borderDash: [6, 4], stepped: "before" }),
  line("walls", C.bad, { borderWidth: 1, borderDash: [2, 3], stepped: "before", quiet: true }),
  line("width", C.accent, { borderWidth: 2 }),
], { ticks: true, main: true, y: { ticks: { maxTicksLimit: 7, callback: v => Number(v).toFixed(2) } } });
widthChart.options.plugins.tooltip.callbacks.label = item => `${item.dataset.label} ${Number(item.parsed.y).toFixed(3)} mm`;
widthChart.options.plugins.tooltip.callbacks.afterBody = items => {
  const r = items.length ? rowAt(items[0].parsed.x) : null;
  if (!r) return [];
  const zone = isNum(r.target_mm) ? `zone ${fmt(r.target_mm - r.band_below_mm, 2)}–${fmt(r.target_mm + r.band_above_mm, 2)} mm` : "no target";
  return [zone, isNum(r.sigma_mm) ? `± ${fmt(r.sigma_mm, 3)} mm · conf ${fmt(r.confidence, 2)}` : "", r.auto ? "Auto on" : "Auto off"].filter(Boolean);
};
const leverChart = (id, label, color, extra) => makeChart(id, [
  line(label, color, { stepped: "before", ...extra })], {});
const flowChart = leverChart("c-flow", "flow %", C.silk);
const speedChart = leverChart("c-speed", "speed %", C.silk);
const nozzleChart = makeChart("c-nozzle", [line("target °C", C.silk, { stepped: "before" }),
  line("actual °C", C.pending, { borderWidth: 1.2 })], { ticks: true });
for (const ch of [flowChart, speedChart, nozzleChart]) {
  ch.options.plugins.tooltip.callbacks.label = item => `${item.dataset.label.split(" ")[0]} ${fmt(item.parsed.y, 1)} ${item.dataset.label.split(" ")[1]}`;
}

function rowAt(t) {
  let lo = 0, hi = rows.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (rows[mid].t_ms <= t) lo = mid; else hi = mid - 1; }
  return rows[lo] || null;
}

function autoLanes(visible) {
  const lanes = [];
  let start = null, dry = false;
  for (const r of visible) {
    if (r.auto && start === null) { start = r.t_ms; dry = r.dry === true; }
    if (!r.auto && start !== null) { lanes.push([start, r.t_ms, dry]); start = null; }
  }
  if (start !== null) lanes.push([start, visible[visible.length - 1].t_ms, dry]);
  return lanes;
}

function redraw() {
  const now = serverNow(), t0 = now - windowMin * 60_000;
  const visible = rows.filter(r => r.t_ms >= t0);
  const pts = pick => visible.map(r => ({ x: r.t_ms, y: pick(r) ?? null }));
  const [zMax, zMin, nHi, nLo, target, walls, width] = widthChart.data.datasets;
  const hasT = r => isNum(r.target_mm);
  zMax.data = pts(r => (hasT(r) ? r.target_mm + r.band_above_mm : null));
  zMin.data = pts(r => (hasT(r) ? r.target_mm - r.band_below_mm : null));
  nHi.data = pts(r => (isNum(r.mm) && isNum(r.sigma_mm) ? r.mm + r.sigma_mm : null));
  nLo.data = pts(r => (isNum(r.mm) && isNum(r.sigma_mm) ? r.mm - r.sigma_mm : null));
  target.data = pts(r => r.target_mm);
  width.data = pts(r => r.mm);
  const floorMm = wallsMm();
  walls.data = isNum(floorMm) ? [...pts(r => (isNum(r.min_width_mm) ? r.min_width_mm : floorMm)), { x: now, y: floorMm }] : [];
  // Keep the zone, the target and the walls line in view, and a little room around the width.
  const dots = frames.filter(f => isNum(f.mm) && f.t_ms >= t0);
  const ys = [...width.data, ...zMin.data, ...zMax.data].map(p => p.y).filter(isNum);
  if (dots.length > 4) {  // the dots' 5th-95th percentile: a stray frame must not squash the plot
    const sorted = dots.map(d => d.mm).sort((p, q) => p - q);
    ys.push(sorted[Math.floor(sorted.length * 0.05)], sorted[Math.floor(sorted.length * 0.95)]);
  }
  ys.push(...walls.data.map(p => p.y).filter(isNum));
  const lo = ys.length ? Math.min(...ys) : 1.2, hi = ys.length ? Math.max(...ys) : 1.8;
  const pad = Math.max(0.05, (hi - lo) * 0.12);
  Object.assign(widthChart.options.scales.y, { min: Math.floor((lo - pad) * 20) / 20, max: Math.ceil((hi + pad) * 20) / 20 });
  for (const [ch, span] of [[flowChart, 10], [speedChart, 10], [nozzleChart, 6]]) {
    const v = visible.flatMap(r => (ch === nozzleChart ? [r.nozzle_target, r.nozzle_c] : [ch === flowChart ? r.flow : r.speed])).filter(isNum);
    const mid = v.length ? (Math.min(...v) + Math.max(...v)) / 2 : 100, half = Math.max(span, v.length ? Math.max(...v) - Math.min(...v) : 0) / 2 + span * 0.1;
    Object.assign(ch.options.scales.y, { min: Math.floor(mid - half), max: Math.ceil(mid + half) });
  }
  flowChart.data.datasets[0].data = pts(r => r.flow);
  speedChart.data.datasets[0].data = pts(r => r.speed);
  nozzleChart.data.datasets[0].data = pts(r => r.nozzle_target);
  nozzleChart.data.datasets[1].data = pts(r => r.nozzle_c);
  const lanes = autoLanes(visible);
  const marks = allEvents().filter(e => e.mark && e.t >= t0).map(e => ({ t: e.t, kind: e.mark, label: e.short }));
  for (const ch of [widthChart, flowChart, speedChart, nozzleChart]) {
    Object.assign(ch.options.scales.x, { min: t0, max: now });
    ch.options.plugins.bench.lanes = lanes;
    ch.options.plugins.bench.marks = marks;
    ch.options.plugins.bench.dots = ch === widthChart ? dots : null;
    ch.update("none");
  }
  $("#win-label").textContent = `last ${windowMin} min`;
  document.querySelectorAll("[data-win]").forEach(b => b.setAttribute("aria-pressed", String(+b.dataset.win === windowMin)));
}
document.querySelectorAll("[data-win]").forEach(b => (b.onclick = () => {
  windowMin = +b.dataset.win;
  prefs.set("bench.window", windowMin);
  redraw();
}));

// ---- trace: rows and events -----------------------------------------------------------------------------------------------
const eventKey = e => `${e.t_ms}|${e.kind}|${JSON.stringify(e)}`;
function addFrames(list) {
  for (const f of list) {
    const k = `${f.t_ms}|${f.group}`;
    if (frameKeys.has(k)) continue;
    frameKeys.add(k);
    frames.push(f);
  }
  if (list.length > 1) frames.sort((p, q) => p.t_ms - q.t_ms);
  const cut = serverNow() - KEEP_MS;
  while (frames.length && frames[0].t_ms < cut) frameKeys.delete(`${frames[0].t_ms}|${frames.shift().group}`);
}

function ingest(newRows, newEvents) {
  for (const e of newEvents) serverEvents.set(eventKey(e), e);
  for (const r of newRows) {
    const prev = lastRow();
    if (prev && r.t_ms <= prev.t_ms) continue;
    if (prev) derive(prev, r);
    rows.push(r);
  }
  const cut = serverNow() - KEEP_MS;
  while (rows.length && rows[0].t_ms < cut) rows.shift();
  derived = derived.filter(e => e.t >= cut);
  for (const [k, e] of serverEvents) if (e.t_ms < cut) serverEvents.delete(k);
}

// Auto's sends, refusals, alarms and outside changes are read off consecutive rows; the operator's own changes come
// from the server's events.
function derive(prev, r) {
  if (r.action && r.sent === true) {
    const from = prev[BY_OP[r.action.operation]?.key];
    derived.push({ t: r.t_ms, tag: "auto", mark: "auto", op: r.action.operation, value: r.action.value,
      text: `${moveText(r.action.operation, from, r.action.value)}${r.floor ? " · floor rules" : ""}${isNum(r.predicted_mm) ? ` · predicted ${fmt(r.predicted_mm, 2)} mm` : ""}`,
      short: `${nameOf(r.action.operation)} ${fmt(r.action.value, 0)}` });
  } else if (r.action && r.sent === false && !(prev.action && prev.sent === false && prev.reason === r.reason)) {
    derived.push({ t: r.t_ms, tag: "refused", text: `${moveText(r.action.operation, null, r.action.value)} not sent: ${(r.reason || "").replace(/^.*not sent:\s*/, "")}` });
  }
  const alarm = a => (a || "").replace(WALLS, m => m.replace(/;?\s*$/, "")).match(/^alarm:\s*(.*?)(;|$)/)?.[1] || null;
  const now = alarm(r.reason), before = alarm(prev.reason);
  if (now && now !== before && !derived.some(e => e.tag === "alarm" && e.text === now && r.t_ms - e.t < 30_000)) {
    derived.push({ t: r.t_ms, tag: "alarm", text: now });
  }
  for (const l of LEVERS) {
    const a = prev[l.key], b = r[l.key];
    if (!isNum(a) || !isNum(b) || Math.abs(a - b) < 0.5) continue;
    const near = e => r.t_ms - e.t <= MATCH_MS && e.op === l.op && Math.abs(e.value - b) < 0.5;
    if (derived.some(e => e.tag === "auto" && near(e))) continue;
    if ([...serverEvents.values()].some(e => e.kind === "manual" && near({ t: e.t_ms, op: e.operation, value: e.value }))) continue;
    derived.push({ t: r.t_ms, tag: "outside", mark: "outside", text: `${moveText(l.op, a, b)} (readback; not this app)`,
      short: `${nameOf(l.op)} ${fmt(b, 0)}` });
  }
}

function serverEventView(e) {
  const t = e.t_ms;
  if (e.kind === "manual") return { t, tag: "you", mark: "manual", text: `set ${moveText(e.operation, null, e.value)}`,
    short: `${nameOf(e.operation)} ${fmt(e.value, 0)}` };
  if (e.kind === "auto") return { t, tag: "auto-switch", text: e.auto ? "Auto switched on" : "Auto switched off" };
  if (e.kind === "target") return { t, tag: "zone", text: isNum(e.target_mm) ? `target ${fmt(e.target_mm, 2)} mm` : "target cleared" };
  if (e.kind === "band") {
    const side = v => (isNum(v) ? fmt(v, 2) : "default");
    return { t, tag: "zone", text: `zone −${side(e.band_below_mm)} / +${side(e.band_above_mm)} mm around the target` };
  }
  if (e.kind === "settings") {
    const set = Object.entries(e.settings || {}).map(([k, v]) => `${SETTING_BY_KEY[k]?.short ?? k} ${settingText(k, v)}`);
    return { t, tag: "settings", text: `Auto settings: ${set.length ? set.join(", ") : "all from the deployment"}` };
  }
  if (e.kind === "levers") {
    const parts = Object.entries(e.levers || {}).map(([op, s]) => `${nameOf(op)} ${s.enabled ? `≤ ${s.max_step ?? (control?.limits?.levers?.[op]?.base_max_step ?? "max")}` : "off"}`);
    return { t, tag: "levers", text: `Auto levers: ${parts.join(", ")}` };
  }
  return { t, tag: e.kind, text: JSON.stringify(e) };
}
// A burst of zone or lever clicks is one change: only the last of a kind within 5 s is listed.
function settledEvents() {
  const list = [...serverEvents.values()].sort((a, b) => a.t_ms - b.t_ms);
  return list.filter((e, i) => !["levers", "band", "settings"].includes(e.kind) ||
    !list.slice(i + 1).some(n => n.kind === e.kind && n.t_ms - e.t_ms <= 5000));
}
const allEvents = () => [...derived, ...settledEvents().map(serverEventView)].sort((a, b) => a.t - b.t);

let tracePolling = false;
async function pollTrace() {
  if (tracePolling) return;
  tracePolling = true;
  try {
    const q = lastRowT === null ? "" : `?since_ms=${lastRowT - 30_000}`;  // events can be up to a tick ahead of rows
    const res = await fetch(`api/trace${q}`, { signal: AbortSignal.timeout(3000) });
    const t = await res.json();
    if (!res.ok || !Array.isArray(t.rows)) throw new Error(`HTTP ${res.status}`);
    serverOffset = isNum(t.server_ms) ? t.server_ms - Date.now() : 0;
    ingest(t.rows, t.events || []);
    addFrames(t.samples || []);
    if (rows.length) lastRowT = lastRow().t_ms;
    traceOkAt = Date.now();
    traceFailed = false;
  } catch {
    traceFailed = true;
  } finally {
    tracePolling = false;
  }
  renderAll();
}

// ---- frames: how many are measured, and why the rest are not --------------------------------------------------------------
const REASON = { ok: "measured", turning: "turning", too_wide: "too wide", axis_out_of_frame: "out of view", ambiguous: "two beads",
  low_contrast: "faint", no_edges: "no edges", not_extruding: "not extruding", no_motion: "no motion data",
  not_converted: "no calibration", no_timestamp: "no timestamp", bad_image: "bad image", no_geometry: "no geometry" };
function renderYield() {
  const now = serverNow(), last = frames.filter(f => now - f.t_ms <= 60_000 && f.reason !== "not_extruding");
  const el = $("#yield");
  if (!last.length) { el.textContent = "no frames while extruding in the last minute"; el.title = ""; return; }
  const counts = {};
  for (const f of last) { const k = isNum(f.mm) ? "ok" : f.reason; counts[k] = (counts[k] || 0) + 1; }
  const pct = k => Math.round(100 * (counts[k] || 0) / last.length);
  const span = Math.max(1, (now - last[0].t_ms) / 1000);
  const rest = Object.keys(counts).filter(k => k !== "ok").sort((p, q) => counts[q] - counts[p]);
  el.replaceChildren(node("b", "", `${pct("ok")} % measured`),
    node("span", "", ` · ${(last.length / span).toFixed(1)} frames/s · `),
    node("span", "muted", rest.slice(0, 3).map(k => `${REASON[k] || k} ${pct(k)} %`).join(" · ")));
  el.title = `Last minute, ${last.length} frames while extruding:\n` + Object.keys(counts).map(k => `${REASON[k] || k}: ${counts[k]}`).join("\n");
}

// ---- width now, zone meter --------------------------------------------------------------------------------------------
// Live pushes; the 1 s poll only backfills what a dropped socket missed.
on("sample", m => { addFrames([m]); renderYield(); renderDial(); });
on("trace", m => { if (lastRowT !== null) { ingest([m], []); lastRowT = lastRow().t_ms; traceOkAt = Date.now(); renderAll(); } });
on("width", m => {
  estimate = isNum(m.mm) ? { ...m, at: Date.now() - (isNum(m.age_ms) ? m.age_ms : 0) } : estimate;
});
on("control", c => {
  if (c.offline) { controlFailed = true; return renderAll(); }
  controlFailed = false;
  controlOkAt = Date.now();
  control = c;
  if (c.estimate && isNum(c.estimate.mm) && (!estimate || Date.now() - (c.estimate.age_ms || 0) > estimate.at)) {
    estimate = { ...c.estimate, at: Date.now() - (c.estimate.age_ms || 0) };
  }
  renderAll();
});

function zoneOf(mm) {
  if (!isNum(mm) || !controlOk() || !isNum(control.target_mm)) return "none";
  const [below, above] = bandOf(control), floor = wallsMm();
  if (isNum(floor) && mm < floor) return "walls";
  if (mm < control.target_mm - below - 1e-9) return "below";
  if (mm > control.target_mm + above + 1e-9) return "above";
  return "inside";
}
const ZONE_TEXT = { inside: ["ok", "in zone"], below: ["warn", "below zone"], above: ["warn", "above zone"],
  walls: ["bad", "below the walls limit"], none: ["off", "no target"] };

function renderDial() {
  const e = liveEstimate(), dial = $("#dial");
  if (!e) {
    $("#w-value").textContent = "—";
    led($("#w-led"), estimate ? "estimate stale" : "no estimate", "off");
    $("#w-delta").textContent = "";
    dial.dataset.zone = "none";
    renderLastFrame();
    return;
  }
  const zone = zoneOf(e.mm), [state, text] = ZONE_TEXT[zone];
  $("#w-value").innerHTML = `${fmt(e.mm, 2)}<small>${isNum(e.sigma_mm) ? `± ${fmt(e.sigma_mm, 2)}` : ""} mm</small>`;
  led($("#w-led"), zone === "none" ? `conf ${fmt(e.confidence, 2)}` : text, state);
  $("#w-delta").textContent = controlOk() && isNum(control.target_mm) ? `${signed(e.mm - control.target_mm)} to target · conf ${fmt(e.confidence, 2)}` : "";
  dial.dataset.zone = zone;
  renderLastFrame();
}
function renderLastFrame() {
  const f = [...frames].reverse().find(x => isNum(x.mm)), age = f ? (serverNow() - f.t_ms) / 1000 : null;
  $("#w-frame").textContent = f && age < 30 ? `last frame ${fmt(f.mm, 2)} mm · ${age.toFixed(1)} s ago` : "no measured frame in 30 s";
}

function renderMeter() {
  const m = $("#zone-meter"), t = controlOk() ? control.target_mm : null, e = liveEstimate();
  m.dataset.empty = isNum(t) ? "" : "1";
  if (!isNum(t)) return;
  const [below, above] = bandOf(control), floor = wallsMm();
  const lo = Math.min(t - below, isNum(floor) ? floor : t, e ? e.mm : t) - 0.1;
  const hi = Math.max(t + above, e ? e.mm : t) + 0.1;
  const lo5 = Math.floor(lo * 20) / 20, hi5 = Math.ceil(hi * 20) / 20, f = v => `${clamp((v - lo5) / (hi5 - lo5), 0, 1) * 100}%`;
  m.style.setProperty("--b0", f(t - below));
  m.style.setProperty("--b1", f(t + above));
  m.style.setProperty("--t", f(t));
  m.style.setProperty("--w", isNum(floor) ? f(floor) : "-10%");
  m.querySelector(".zm-needle").hidden = !e;
  if (e) {
    m.style.setProperty("--n", f(e.mm));
    m.style.setProperty("--s0", f(e.mm - (e.sigma_mm || 0)));
    m.style.setProperty("--s1", f(e.mm + (e.sigma_mm || 0)));
    m.querySelector(".zm-needle").dataset.zone = zoneOf(e.mm);
  }
  const step = hi5 - lo5 > 0.6 ? 0.2 : 0.1, marks = [];
  for (let v = Math.ceil(lo5 / step) * step; v <= hi5 + 1e-9; v += step) marks.push(v);
  const scale = m.querySelector(".zm-scale"), sig = marks.map(v => v.toFixed(2)).join() + lo5 + hi5;
  if (scale.dataset.sig !== sig) {
    scale.dataset.sig = sig;
    scale.replaceChildren(...marks.map(v => Object.assign(node("span", "", v.toFixed(1 + (step < 0.15))), { style: `left:${f(v)}` })));
  }
}

// ---- zone inputs: min / target / max (min and max are stored as distances from the target, so they move with it) ---------
const zoneInputs = { min: $("#z-min"), target: $("#z-target"), max: $("#z-max") };
const editing = new Set();
let zoneMsgTimer = 0;
function zoneMsg(text, state = "bad") {
  const el = $("#zone-msg");
  clearTimeout(zoneMsgTimer);
  el.textContent = text;
  el.dataset.state = text ? state : "";
  if (text) zoneMsgTimer = setTimeout(() => zoneMsg(""), MSG_MS);
}

function renderZone() {
  const up = controlOk(), t = up ? control.target_mm : null, [below, above] = bandOf(control);
  const custom = up && (isNum(control.band_below_mm) || isNum(control.band_above_mm));
  const shown = { target: isNum(t) ? t : null, min: isNum(t) ? t - below : null, max: isNum(t) ? t + above : null };
  for (const [k, el] of Object.entries(zoneInputs)) {
    el.disabled = !up || (k !== "target" && !isNum(t)) || (k === "target" && control?.relative === true);
    if (!editing.has(k)) el.value = isNum(shown[k]) ? mmText(shown[k]) : "";
  }
  zoneInputs.min.placeholder = zoneInputs.max.placeholder = isNum(t) ? "" : "—";
  zoneInputs.target.placeholder = "none";
  $("#z-min-rel").textContent = isNum(t) ? `−${fmt(below, 2)}${custom ? "" : " default"}` : "";
  $("#z-max-rel").textContent = isNum(t) ? `+${fmt(above, 2)}${custom ? "" : " default"}` : "";
  $("#z-target-rel").textContent = up ? "mm" : "offline";
  document.querySelectorAll("[data-band]").forEach(b => {
    b.disabled = !up || !isNum(t);
    b.setAttribute("aria-pressed", String(custom && Math.abs(below - +b.dataset.band) < 1e-6 && Math.abs(above - +b.dataset.band) < 1e-6));
  });
  $("#z-hold").disabled = !up;
  const noise = liveRow()?.noise_mm;
  const walls = wallsMm();
  const zoneNote = isNum(t) && isNum(walls) && walls > t - below + 1e-9
    ? `The walls limit ${fmt(walls, 2)} mm is above your zone's min: below it Auto acts at once, with bigger steps.`
    : isNum(t) && isNum(noise) && noise > Math.min(below, above) + 1e-9
      ? `Measurement noise is ±${fmt(noise, 2)} mm, wider than your zone: Auto also holds within the noise.` : "";
  if (!$("#zone-msg").dataset.state || $("#zone-msg").dataset.state === "info") {
    $("#zone-msg").textContent = zoneNote;
    $("#zone-msg").dataset.state = zoneNote ? "info" : "";
  }
}

async function commitZone(k) {
  const el = zoneInputs[k];
  editing.delete(k);
  if (el.validity.badInput) { zoneMsg("Not set: not a number"); return renderZone(); }
  const raw = el.value.trim(), t = control?.target_mm;
  let body;
  if (k === "target") body = { target_mm: raw === "" ? null : Number(raw) };
  else {
    if (raw === "") body = { [k === "min" ? "band_below_mm" : "band_above_mm"]: null };
    else {
      const d = k === "min" ? round3(t - Number(raw)) : round3(Number(raw) - t);
      if (d < 0) { zoneMsg(`Not set: ${k} must be ${k === "min" ? "at or below" : "at or above"} the target`); return renderZone(); }
      body = { [k === "min" ? "band_below_mm" : "band_above_mm"]: d };
    }
  }
  const r = await postControl(body);
  zoneMsg(r.error ? (r.lost ? r.error : `Not set: ${r.error}`) : "");
  renderZone();
}
for (const [k, el] of Object.entries(zoneInputs)) {
  el.oninput = () => editing.add(k);
  el.onblur = () => { if (editing.has(k)) commitZone(k); };
  el.onkeydown = e => {
    if (e.key === "Enter") el.blur();
    else if (e.key === "Escape") { editing.delete(k); renderZone(); el.blur(); }
  };
}
document.querySelectorAll("[data-band]").forEach(b => (b.onclick = async () => {
  const v = +b.dataset.band, r = await postControl({ band_below_mm: v, band_above_mm: v });
  zoneMsg(r.error ? `Not set: ${r.error}` : "");
}));
$("#z-hold").onclick = async () => {
  const r = await postControl({ hold_current: true });
  zoneMsg(r.error ? `Not set: ${r.error}` : "");
};

const wallsInput = $("#z-walls");
function renderWalls() {
  const up = controlOk(), mine = control?.settings?.min_width_mm, base = control?.limits?.defaults?.min_width_mm;
  wallsInput.disabled = !up;
  if (!editing.has("walls")) wallsInput.value = isNum(wallsMm()) ? mmText(wallsMm()) : "";
  wallsInput.closest(".zf").dataset.custom = isNum(mine) ? "1" : "";
  $("#z-walls-rel").textContent = !up ? "" : isNum(mine) ? (isNum(base) ? `default ${fmt(base, 2)}` : "yours") : "default";
  $("#legend-walls").textContent = isNum(wallsMm()) ? `${fmt(wallsMm(), 2)} walls` : "walls";
}
wallsInput.oninput = () => editing.add("walls");
wallsInput.onblur = async () => {
  if (!editing.has("walls")) return;
  editing.delete("walls");
  if (wallsInput.validity.badInput) { zoneMsg("Not set: not a number"); return renderWalls(); }
  const raw = wallsInput.value.trim();
  const r = await postControl({ settings: { min_width_mm: raw === "" ? null : Number(raw) } });
  zoneMsg(r.error ? (r.lost ? r.error : `Not set: ${r.error}`) : "");
  renderWalls();
};
wallsInput.onkeydown = e => {
  if (e.key === "Enter") wallsInput.blur();
  else if (e.key === "Escape") { editing.delete("walls"); renderWalls(); wallsInput.blur(); }
};

// ---- Auto switch ----------------------------------------------------------------------------------------------------------
function renderAuto() {
  const btn = $("#auto"), up = controlOk(), r = liveRow();
  btn.disabled = !up || btn.dataset.busy === "1";
  btn.setAttribute("aria-checked", String(up && control.auto === true));
  if (!up) {
    led($("#auto-led"), "controller offline", "bad");
    led($("#send-led"), "sends ?", "off");
    led($("#gate-led"), "gate ?", "off");
    return;
  }
  const alarm = r && /^alarm:/.test((r.reason || "").replace(WALLS, ""));
  led($("#auto-led"), !control.auto ? "advice only" : !control.send_enabled ? "auto · dry run" : alarm ? "auto · alarm" : "auto · acting",
    !control.auto ? "off" : !control.send_enabled || alarm ? "warn" : "ok");
  led($("#send-led"), control.send_enabled ? "sends on" : "sends off", control.send_enabled ? "ok" : "off");
  led($("#gate-led"), !r ? "gate ?" : r.gate_open ? "gate open" : "gate closed", !r ? "off" : r.gate_open ? "ok" : "warn");
  $("#gate-led").title = r && !r.gate_open ? "LEAM's command gate is closed: their operator must put native DEMEX in RUN" : "LEAM's command gate";
}
$("#auto").onclick = async () => {
  const btn = $("#auto");
  if (!controlOk() || btn.dataset.busy === "1") return;
  btn.dataset.busy = "1";
  renderAuto();
  const want = !control.auto, r = await postControl({ auto: want });
  btn.dataset.busy = "";
  const msg = $("#auto-msg");
  msg.textContent = r.error ? (r.lost ? r.error : `Not set: ${r.error}`) : "";
  msg.dataset.state = r.error ? "bad" : "";
  renderAuto();
};

// ---- Auto loop: what it is doing now, in plain words ----------------------------------------------------------------------
const STEPS = [["Measure", "IR width"], ["Compare", "your zone"], ["Confirm", "is it real?"], ["Act", "one step"],
  ["Settle", "effect"]];
$("#steps").replaceChildren(...STEPS.map(([name, sub]) => {
  const li = node("li");
  li.append(node("b", "", name), node("span", "", sub));
  return li;
}));

const ALARMS = [
  [/no usable lever can move the width/, "No lever Auto may use can move the width",
    "the printer model predicts too little from the levers that are on and not at a limit (flow barely matters while the hotend is at its melt limit; speed stops at 100 %). Switch another lever on, or change the target."],
  [/target out of reach/, "Target out of reach", "even the allowed levers at their limits can't get there: change the target or allow more levers"],
  [/not applied by the printer/, "The printer didn't apply Auto's command", "confirmed by LEAM's gateway, but the readback never changed: switch Auto off and on to re-arm"],
  [/commands in a row failed/, "Auto's commands keep failing", "switch Auto off and on to re-arm once the cause is fixed"],
  [/widening had no visible effect/, "Widening did nothing visible", "the camera may be saturated or the lever ineffective: check the bead, lower the target, or re-arm Auto"],
  [/moved the width less than/, "That lever barely moves the width", "Auto stopped using it: switch Auto off and on to re-arm"],
  [/target set under another camera model/, "Set the target again", "it was set under a different camera calibration"],
];

function explain(r) {
  let raw = r.reason || "";
  const walls = WALLS.exec(raw);
  if (walls) raw = raw.slice(walls[0].length);
  const floor = /^floor rules:\s*/.test(raw);
  raw = raw.replace(/^floor rules:\s*/, "");
  const out = (step, state, title, detail = "", progress = null) => ({ step, state, title, detail, progress, walls: walls?.[1], floor });
  const act = r.action ? moveText(r.action.operation, rows.length > 1 ? rows[rows.length - 2][BY_OP[r.action.operation]?.key] : null, r.action.value) : "";
  const pred = isNum(r.predicted_mm) ? `model predicts ${fmt(r.predicted_mm, 2)} mm` : "";
  let m;
  if (r.action && r.sent === true) return out(3, "active", `Sent ${act}`, pred);
  if (r.action && r.sent === false) return out(3, "blocked", `Wanted ${act}, not sent`, raw.replace(/^not sent:\s*/, ""));
  if (r.action && r.dry) return out(3, "advice", `Would set ${act}`, "dry run: this device doesn't send commands");
  if (r.action && !r.auto) return out(3, "advice", `Would set ${act}`, `Auto is off · ${pred}`);
  if ((m = /^gate closed \(would set (.+)\)/.exec(raw))) return out(3, "blocked", `Would set ${m[1]}`, "LEAM's command gate is closed: their operator must put DEMEX in RUN");
  if ((m = /^off target ([+-][\d.]+) mm, confirming \((\d+)\/(\d+) s, (\d+)\/(\d+) estimates\)/.exec(raw))) {
    const err = +m[1];
    return out(2, "active", `${Math.abs(err).toFixed(2)} mm ${err > 0 ? "too narrow" : "too wide"}: checking it's real`,
      `outside the zone for ${m[2]} of ${m[3]} s, ${m[4]} of ${m[5]} estimates`, Math.min(m[2] / m[3], m[4] / m[5]));
  }
  if ((m = /^waiting for (.+) to be confirmed/.exec(raw))) return out(3, "active", `Sent ${m[1]}`, "waiting for the printer to confirm");
  if ((m = /^waiting for (.+) to take effect \((\d+)\/(\d+) samples/.exec(raw))) {
    return out(4, "active", `${m[1]} is set: waiting for new beads`, `${m[2]} of ${m[3]} measurements of beads laid since the change`, m[2] / m[3]);
  }
  if ((m = /^waiting for (.+?) (?:to show its effect|to settle)/.exec(raw)) || /^waiting for (the width|the heater|enough)/.test(raw)) {
    return out(4, "active", "Letting the width settle", raw);
  }
  if ((m = /^on target \(inside ([\d.]+)-([\d.]+) mm\)/.exec(raw))) return out(1, "ok", "Inside your zone: holding", `zone ${m[1]}–${m[2]} mm`);
  if ((m = /^on target \(\|error\| ([\d.]+) <= noise ([\d.]+) mm\)/.exec(raw))) {
    return out(1, "ok", "Within the measurement noise: holding", `off by ${m[1]} mm, noise ±${m[2]} mm`);
  }
  if (raw === "spacing") return out(3, "wait", "Short pause between commands", "commands are at least 2 s apart");
  if ((m = /;\s*holding (\d+) s$/.exec(raw))) {
    const set = /set (\w+) ([\d.]+)/.exec(raw), lever = set && LEVERS.find(l => l.name.toLowerCase() === set[1]);
    const mine = lever && [...serverEvents.values()].some(e => e.kind === "manual" && e.operation === lever.op &&
      Math.abs(e.value - +set[2]) < 0.5 && r.t_ms - e.t_ms <= 90_000);
    return out(1, "blocked", mine ? `Hands off: you set ${set[1]} ${set[2]}` : "Hands off: someone else changed a setting",
      `${mine ? "" : `${raw.replace(/^alarm:\s*/, "").replace(m[0], "")} · `}Auto waits ${m[1]} s, then leaves ${mine ? set[1] : "that lever"} alone for ${control?.limits?.pin_s ?? "a while"} s`);
  }
  if (/^(no width estimate|width estimate stale)/.test(raw)) {
    return isNum(r.mm)
      ? out(0, "wait", "Waiting for beads laid since the last change", "Auto ignores measurements of beads laid before it was switched on or a setting changed")
      : out(0, "wait", "No measurement right now", "the camera doesn't see a bead it can measure (travel, a turn, the other side)");
  }
  if (/^(low confidence|waiting for a second view|waiting for a fresh estimate)/.test(raw)) {
    return out(0, "wait", "Waiting for a usable measurement", raw);
  }
  if (/^not extruding/.test(raw)) return out(0, "idle", "The printer isn't extruding", "Auto only acts while a bead is being laid");
  if ((m = /^first layers \((.+) of (\d+)\)/.exec(raw))) return out(0, "idle", `First layers: Auto starts at layer ${m[2]}`, `now at layer ${m[1]}`);
  if (/^no target/.test(raw)) return out(1, "idle", "No target set", "type a target width above");
  if (/^no readback/.test(raw)) return out(0, "wait", "Waiting for the printer's readbacks", raw);
  if (/^(no model|no printer model)/.test(raw)) return out(0, "alarm", "A model is missing: Auto can't act", raw);
  if (/^at the camera's ceiling/.test(raw)) return out(1, "blocked", "At the camera's ceiling", raw);
  if (/^alarm:/.test(raw)) {
    const why = raw.replace(/^alarm:\s*/, "");
    const known = ALARMS.find(([re]) => re.test(why));
    return out(/lever|reach|refused|commands in a row|not applied|widening/.test(why) ? 3 : 1, "alarm",
      known ? known[1] : "Alarm", known ? known[2] : why);
  }
  return out(1, "wait", raw || "—", "");
}

function renderLoop() {
  const r = liveRow(), box = $("#loop");
  const steps = [...$("#steps").children];
  if (!r) {
    box.dataset.state = "offline";
    steps.forEach(li => (li.dataset.state = ""));
    $("#loop-title").textContent = linkOk() ? "No fresh step from the control loop" : "Control loop unreachable";
    $("#loop-detail").textContent = rows.length ? `last step ${clock(lastRow().t_ms)}` : "";
    $("#loop-progress").hidden = true;
    $("#loop-raw").textContent = "";
    $("#loop-walls").hidden = true;
    $("#loop-mode").textContent = "—";
    $("#loop-mode").dataset.mode = "";
    return;
  }
  const x = explain(r);
  box.dataset.state = x.state;
  steps.forEach((li, i) => {
    li.dataset.state = i < x.step ? "done" : i === x.step ? x.state : "";
    li.setAttribute("aria-current", i === x.step ? "step" : "false");
  });
  const est = isNum(r.mm) ? `${fmt(r.mm, 2)} ± ${fmt(r.sigma_mm, 2)} mm · conf ${fmt(r.confidence, 2)} · ${r.n} samples` : "no estimate";
  steps[0].querySelector("span").textContent = isNum(r.mm) ? `${fmt(r.mm, 2)} mm` : "IR width";
  steps[0].title = est;
  $("#loop-mode").textContent = !r.auto ? "advice" : r.dry ? "dry run" : "auto";
  $("#loop-mode").dataset.mode = !r.auto ? "advice" : r.dry ? "dry" : "auto";
  $("#loop-title").textContent = x.title;
  $("#loop-detail").textContent = [x.detail, x.floor ? `floor rules: below ${fmt(wallsMm(), 2)} mm Auto acts sooner` : ""].filter(Boolean).join(" · ");
  $("#loop-progress").hidden = x.progress === null;
  if (x.progress !== null) $("#loop-progress").firstElementChild.style.width = `${clamp(x.progress, 0, 1) * 100}%`;
  const said = r.reason || "";  // the controller's own words, unless the detail above already says exactly that
  $("#loop-raw").textContent = said.replace(WALLS, "").replace(/^(alarm|floor rules):\s*/, "") === x.detail ? "" : said;
  $("#loop-raw").title = "The controller's own words for this step";
  $("#loop-walls").hidden = !x.walls;
  $("#loop-walls").textContent = x.walls ? `Below ${x.walls} mm: walls may not touch` : "";
  const last = [...derived].reverse().find(e => e.tag === "auto");
  $("#loop-last").textContent = last ? `Last Auto command ${clock(last.t)} (${ago(last.t)}): ${last.text}` : "No Auto command in the last 15 min";
}
const ago = t => {
  const s = Math.max(0, Math.round((serverNow() - t) / 1000));
  return s < 60 ? `${s} s ago` : `${Math.floor(s / 60)} min ${s % 60} s ago`;
};

// ---- the printer model's view: what one of Auto's steps on each lever would do now -------------------------------------------
$("#model-rows").replaceChildren(...LEVERS.map(l => {
  const li = node("li");
  li.dataset.op = l.op;
  li.append(node("span", "silk", l.name), node("span", "m-up", "—"), node("span", "m-down", "—"));
  return li;
}));
function renderModel() {
  const r = liveRow(), m = r?.model;
  $("#model").dataset.empty = m ? "" : "1";
  if (!m) {
    $("#model-note").textContent = r ? "No prediction: the printer model or a readback is missing." : "";
    return;
  }
  const chosen = r.action && !r.hold ? r.action.operation : null, dead = [];
  for (const l of LEVERS) {
    const li = $(`#model-rows li[data-op="${l.op}"]`), v = m.levers[l.op];
    const cell = (d, sign) => (!v ? "—" : d === null ? "at its limit" : `${sign}${v.step} ${l.unit} → ${signed(d, 2)} mm`);
    li.querySelector(".m-up").textContent = cell(v?.up ?? null, "+");
    li.querySelector(".m-down").textContent = cell(v?.down ?? null, "−");
    const most = v ? Math.max(Math.abs(v.up ?? 0), Math.abs(v.down ?? 0)) : 0;
    li.dataset.dead = v && most < 0.01 ? "1" : "";
    li.dataset.chosen = chosen === l.op ? "1" : "";
    li.dataset.off = control?.levers?.[l.op]?.enabled === false ? "1" : "";
    if (v && most < 0.01) dead.push(l.name.toLowerCase());
  }
  const gap = isNum(r.mm) ? r.mm - m.width_mm : null;
  $("#model-note").textContent = [
    `Model expects ${fmt(m.width_mm, 2)} mm at these settings${isNum(gap) ? `; measured ${signed(gap, 2)} from that` : ""}.`,
    dead.includes("flow") ? "Flow barely moves the width here: the model thinks the hotend is at its melt limit, so more filament doesn't widen the bead." : "",
    "Auto picks the step that best closes half the gap, weighing cost (flow 1, speed 3, nozzle 10).",
  ].filter(Boolean).join(" ");
}

// ---- levers: readback, Auto's switch and step, and manual sends -------------------------------------------------------------
$("#lever-rows").replaceChildren(...LEVERS.map(l => {
  const el = node("div", "lever");
  el.dataset.op = l.op;
  el.innerHTML = `
    <div class="lv-head">
      <span class="lv-name"><span class="silk">${l.name}</span><small>${l.unit}</small></span>
      <span class="lv-now" title="Printer readback">—</span><small class="lv-actual"></small>
      <span class="lv-step" title="The largest move Auto makes on this lever per command">
        <span class="muted">step ≤</span>
        <button type="button" class="mini" data-step="-1" aria-label="Smaller Auto step for ${l.name}">−</button>
        <output>—</output>
        <button type="button" class="mini" data-step="1" aria-label="Larger Auto step for ${l.name}">+</button></span>
      <label class="toggle" title="Let Auto move this lever"><input type="checkbox" checked><span>Auto</span></label>
    </div>
    <div class="lv-manual">
      ${NUDGES.filter(d => d < 0).map(d => `<button type="button" class="mini" data-nudge="${d}">${d}</button>`).join("")}
      <input class="lcd" type="number" min="${l.min}" max="${l.max}" step="1" aria-label="${l.name} value to send">
      ${NUDGES.filter(d => d > 0).map(d => `<button type="button" class="mini" data-nudge="${d}">+${d}</button>`).join("")}
      <button type="button" class="cue">Set</button>
    </div>
    <div class="lv-pin"></div>
    <div class="lv-out" aria-live="polite"></div>`;
  return el;
}));

const pending = {};  // op -> value typed or nudged, not sent yet
const requestOp = new Map();
const readback = l => { const r = liveRow(); return r && isNum(r[l.key]) ? r[l.key] : null; };
function setPending(l, v) {
  pending[l.op] = v === null ? null : clamp(Math.round(v), l.min, l.max);
  renderLevers();
}
function setOutcome(op, state, text) {
  const o = $(`.lever[data-op="${op}"] .lv-out`);
  o.dataset.state = state;
  o.textContent = text;
}
for (const l of LEVERS) {
  const el = $(`.lever[data-op="${l.op}"]`), lcd = el.querySelector(".lcd");
  el.querySelectorAll("[data-nudge]").forEach(b => (b.onclick = () => {
    const base = isNum(pending[l.op]) ? pending[l.op] : readback(l);
    if (isNum(base)) setPending(l, base + +b.dataset.nudge);
  }));
  lcd.oninput = () => { pending[l.op] = lcd.value === "" ? null : Number(lcd.value); el.dataset.edited = lcd.value === "" ? "" : "1"; };
  lcd.onkeydown = e => {
    if (e.key === "Enter") el.querySelector(".cue").click();
    if (e.key === "Escape") setPending(l, null);
  };
  el.querySelector(".cue").onclick = async () => {
    const v = pending[l.op];
    if (!isNum(v)) return setOutcome(l.op, "bad", "type or nudge a value first");
    let r;
    try { r = await api("api/request", { operation: l.op, value: v }); } catch { r = { ok: false, error: "app offline" }; }
    if (r.ok) {
      requestOp.set(r.request?.request_id, l.op);
      setOutcome(l.op, "pending", `sent ${fmt(v, 0)} ${l.unit}, waiting for the printer`);
      setPending(l, null);
    } else setOutcome(l.op, "bad", `not sent: ${r.error}`);
  };
  el.querySelector(".toggle input").onchange = async e => {
    const r = await postControl({ levers: { [l.op]: { enabled: e.target.checked } } });
    if (r.error) setOutcome(l.op, "bad", r.error);
    renderLevers();
  };
  el.querySelectorAll("[data-step]").forEach(b => (b.onclick = async () => {
    const lim = control?.limits?.levers?.[l.op];
    if (!lim) return;
    const next = clamp(stepOf(l.op) + +b.dataset.step, 1, lim.base_max_step);
    const r = await postControl({ levers: { [l.op]: { max_step: next === lim.base_max_step ? null : next } } });
    if (r.error) setOutcome(l.op, "bad", r.error);
  }));
}
on("writer_log", e => {
  const p = e.payload || {};
  if (e.kind === "result" && requestOp.has(p.request_id)) {
    const op = requestOp.get(p.request_id);
    if (p.state === "setting_confirmed") setOutcome(op, "ok", `✓ confirmed ${p.observation?.observed_value ?? ""} ${unitOf(op)}`);
    else if (p.state === "rejected") setOutcome(op, "bad", `✕ rejected${p.reason ? `: ${p.reason}` : ""}`);
    else if (p.state !== "accepted") setOutcome(op, "warn", `? ${p.state}`);
  }
});

// The operator's tuning answers at once (the POST returns it); limits only follow at the controller's next step.
const stepOf = op => control?.levers?.[op]?.max_step ?? control?.limits?.levers?.[op]?.base_max_step;

function renderLevers() {
  const up = controlOk(), lim = control?.limits?.levers || {}, tuned = control?.levers || {};
  for (const l of LEVERS) {
    const el = $(`.lever[data-op="${l.op}"]`), rb = readback(l), lv = lim[l.op], t = tuned[l.op];
    el.querySelector(".lv-now").textContent = isNum(rb) ? `${fmt(rb, 0)} ${l.unit}` : "—";
    const c = l.op === "nozzle_temperature_c" ? liveRow()?.nozzle_c : null;
    el.querySelector(".lv-actual").textContent = isNum(c) ? `actual ${fmt(c, 1)}` : "";
    const box = el.querySelector(".toggle input");
    box.checked = t ? t.enabled !== false : true;
    box.disabled = !up;
    el.dataset.off = box.checked ? "" : "1";
    const step = stepOf(l.op);
    el.querySelector(".lv-step output").textContent = lv ? `${step}` : "—";
    el.querySelector(".lv-step").dataset.tuned = lv && step < lv.base_max_step ? "1" : "";
    el.querySelector(".lv-step").title = lv ? `Auto moves ${l.name.toLowerCase()} by at most ${step} ${l.unit} per command (normal ${lv.base_max_step})` : "";
    el.querySelectorAll("[data-step]").forEach(b => {
      b.disabled = !up || !lv || (+b.dataset.step < 0 ? step <= 1 : step >= lv.base_max_step);
    });
    const pin = control?.pinned?.[l.op], left = pin && isNum(pin.until_ms) ? Math.ceil((pin.until_ms - serverNow()) / 1000) : 0;
    el.querySelector(".lv-pin").textContent = left > 0 ? `Auto hands off for ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}${isNum(pin.value) ? ` (set to ${fmt(pin.value, 0)} outside Auto)` : ""}` : "";
    el.querySelector(".lv-pin").title = left > 0 ? "Set from outside Auto: Auto leaves this lever alone until the countdown ends" : "";
    const lcd = el.querySelector(".lcd");
    if (document.activeElement !== lcd) lcd.value = isNum(pending[l.op]) ? pending[l.op] : isNum(rb) ? rb : "";
    el.dataset.edited = isNum(pending[l.op]) && pending[l.op] !== rb ? "1" : "";
    el.querySelector(".cue").textContent = isNum(pending[l.op]) && isNum(rb) && pending[l.op] !== rb
      ? `Set ${signed(pending[l.op] - rb, 0)}` : "Set";
  }
  const limits = control?.limits;
  $("#lever-hint").textContent = limits?.pin_s ? `your change: Auto hands off ${limits.pin_s} s` : "";
  $("#lever-note").textContent = limits?.levers
    ? `Set sends to the printer now. Auto moves one lever per step, by at most its step, and only towards the target; with Auto on, a lever you set is left alone for ${limits.pin_s} s and Auto pauses ${limits.foreign_hold_s} s.`
    : "Auto's limits are unknown until the models are loaded.";
}

// ---- Auto settings: the operator's values over the deployment's (control_config.OPERATOR has the server's ranges) ------------
const SETTINGS = [
  { key: "persist_s", name: "Confirm for", short: "confirm", unit: "s", min: 0, max: 60, step: 1,
    why: "how long the width must stay outside your zone before Auto acts" },
  { key: "persist_estimates", name: "Confirm readings", short: "readings", unit: "", min: 1, max: 20, step: 1,
    why: "and how many width estimates in a row" },
  { key: "kappa", name: "Step size", short: "step", unit: "%", min: 10, max: 100, step: 5, scale: 100,
    why: "share of the gap to the target one command tries to close" },
  { key: "pin_s", name: "Hands off", short: "hands off", unit: "s", min: 60, max: 900, step: 10,
    why: "after you set a lever by hand, Auto leaves it alone this long" },
  { key: "min_layer", name: "First Auto layer", short: "from layer", unit: "", min: 1, max: 50, step: 1,
    why: "Auto acts from this layer on" },
  { key: "floor_step_factor", name: "Walls step", short: "walls step", unit: "×", min: 1, max: 2, step: 0.1,
    why: "below the walls limit, Auto's steps may be this many times bigger" },
];
const SETTING_BY_KEY = { ...Object.fromEntries(SETTINGS.map(s => [s.key, s])), min_width_mm: { short: "walls", unit: "mm" } };
const shownValue = (s, v) => (isNum(v) ? round3(v * (s.scale || 1)) : null);
const settingText = (key, v) => {
  const s = SETTING_BY_KEY[key];
  return s ? `${shownValue(s, v)}${s.unit ? ` ${s.unit}` : ""}` : String(v);
};
$("#set-rows").replaceChildren(...SETTINGS.map(s => {
  const el = node("div", "set-row");
  el.dataset.key = s.key;
  el.innerHTML = `
    <label class="set-name" for="set-${s.key}"><span class="silk">${s.name}</span><small>${s.why}</small></label>
    <input class="lcd" id="set-${s.key}" type="number" min="${s.min}" max="${s.max}" step="${s.step}" inputmode="decimal">
    <span class="set-unit">${s.unit}</span><span class="set-def"><span class="set-base"></span>
      <button type="button" class="mini" title="Back to the deployment's value">reset</button></span>
    <div class="set-out" aria-live="polite"></div>`;
  return el;
}));
const settingEditing = new Set();
async function commitSetting(s, value) {
  const el = $(`.set-row[data-key="${s.key}"]`), out = el.querySelector(".set-out");
  settingEditing.delete(s.key);
  const r = await postControl({ settings: { [s.key]: value } });
  out.textContent = r.error ? (r.lost ? r.error : `Not set: ${r.error}`) : "";
  renderSettings();
}
for (const s of SETTINGS) {
  const el = $(`.set-row[data-key="${s.key}"]`), input = el.querySelector("input");
  input.oninput = () => settingEditing.add(s.key);
  input.onblur = () => {
    if (!settingEditing.has(s.key)) return;
    if (input.validity.badInput) { settingEditing.delete(s.key); el.querySelector(".set-out").textContent = "Not set: not a number"; return renderSettings(); }
    const raw = input.value.trim(), v = Number(raw);
    if (raw !== "" && !(v >= s.min && v <= s.max)) {
      settingEditing.delete(s.key);
      el.querySelector(".set-out").textContent = `Not set: ${s.name.toLowerCase()} must be ${s.min}–${s.max}${s.unit ? ` ${s.unit}` : ""}`;
      return renderSettings();
    }
    commitSetting(s, raw === "" ? null : round3(v / (s.scale || 1)));
  };
  input.onkeydown = e => {
    if (e.key === "Enter") input.blur();
    else if (e.key === "Escape") { settingEditing.delete(s.key); renderSettings(); input.blur(); }
  };
  el.querySelector(".mini").onclick = () => commitSetting(s, null);
}
function renderSettings() {
  const up = controlOk(), mine = control?.settings || {}, base = control?.limits?.defaults || {}, now = control?.limits || {};
  for (const s of SETTINGS) {
    const el = $(`.set-row[data-key="${s.key}"]`), input = el.querySelector("input");
    const v = isNum(mine[s.key]) ? mine[s.key] : isNum(base[s.key]) ? base[s.key] : now[s.key];
    input.disabled = !up;
    if (!settingEditing.has(s.key)) input.value = isNum(v) ? shownValue(s, v) : "";
    el.dataset.custom = isNum(mine[s.key]) ? "1" : "";
    el.querySelector(".set-base").textContent = isNum(mine[s.key]) && isNum(base[s.key]) ? `default ${shownValue(s, base[s.key])}` : "default";
  }
}

// ---- event list ------------------------------------------------------------------------------------------------------------
const TAGS = { auto: "auto", you: "you", outside: "outside", "auto-switch": "auto", zone: "zone", levers: "levers",
  alarm: "alarm", refused: "not sent", settings: "settings" };
let shownEvents = "";
function renderEvents() {
  const list = allEvents().slice(-120).reverse(), sig = list.map(e => `${e.t}${e.text}`).join("|");
  $("#events-count").textContent = list.length ? `${list.length} in 15 min` : "";
  if (sig === shownEvents) return;
  shownEvents = sig;
  $("#events").replaceChildren(...list.map(e => {
    const li = node("li");
    li.dataset.tag = e.tag;
    li.append(node("time", "", clock(e.t)), node("span", "tag", TAGS[e.tag] || e.tag), node("span", "txt", e.text));
    return li;
  }));
}

// ---- everything, each second ---------------------------------------------------------------------------------------------
function renderAll() {
  const up = linkOk();
  document.body.dataset.link = up ? "ok" : "offline";
  const banner = $("#offline");
  banner.hidden = up && (controlOk() || !controlFailed);
  banner.textContent = !up ? (rows.length ? `App unreachable: showing data up to ${clock(lastRow().t_ms)}` : "Connecting to the app…")
    : "Controller state unreachable: zone and Auto can't be changed right now";
  renderVitals();
  renderDial();
  renderMeter();
  renderZone();
  renderWalls();
  renderAuto();
  renderLoop();
  renderLevers();
  renderSettings();
  renderEvents();
  renderModel();
  renderYield();
}

setInterval(pollTrace, 1000);
setInterval(redraw, 1000);
setInterval(() => { renderDial(); renderMeter(); renderVitals(); }, 500);
pollTrace().then(redraw);
connectSocket();
startControlPoll();
