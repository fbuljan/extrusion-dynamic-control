const showIr = cameraView("#ir", "#ir-label", "#ir-empty", "IR");
const onFrame = f => f.camera_kind === "ir" && showIr(f);

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// ---- layout: the control rack width, remembered per browser (older builds also stored camera mode and sizes) -------
const consoleEl = $("#console");
const RACK_MIN = 340, RACK_MAX = 760, RACK_DEFAULT = 440;
const savedRack = prefs.get("layout", {})?.rack;
let rackWidth = typeof savedRack === "number" && savedRack >= RACK_MIN && savedRack <= RACK_MAX ? savedRack : RACK_DEFAULT;

function applyLayout() {
  consoleEl.style.setProperty("--rack-w", `${rackWidth}px`);
  prefs.set("layout", { rack: rackWidth });  // rewriting the key also drops the retired camera-layout fields
}

draggable($("#rack-splitter"), {
  onDrag: e => { rackWidth = clamp(consoleEl.getBoundingClientRect().right - e.clientX, RACK_MIN, RACK_MAX); applyLayout(); },
  onKey: step => { rackWidth = clamp(rackWidth - step * 16, RACK_MIN, RACK_MAX); applyLayout(); },
  onReset: () => { rackWidth = RACK_DEFAULT; applyLayout(); },
});
applyLayout();

// ---- meter bridge: every live readout in one strip --------------------------------------------------------------
const STALE_MS = 1000;  // guide: telemetry expires 1 s after publish, and nothing may be shown past that
let lastTelemetryAt = 0;

const unitHtml = (text, unit) => (text === "—" || text === "unknown" ? text : `${text}<small>${unit}</small>`);
const heater = h => !h ? { value: "—", sub: "no valid reading" }
  : { value: unitHtml(fmt(h.current, 1), "°C"), sub: `target ${fmt(h.target, 0)}` + (h.state !== "active" ? ` · ${h.state}` : ""),
      warn: h.state !== "active" };
const BRIDGE = [
  { id: "nozzle", label: "Nozzle", read: m => heater(m.nozzle) },
  { id: "bed", label: "Bed", read: m => heater(m.bed) },
  { id: "melt", label: "Melt · IR", read: m => m.melt !== null
    ? { value: unitHtml(fmt(m.melt, 1), "°C"), sub: "fresh bead" } : { value: "—", sub: m.melt_reason || "invalid" } },
  { id: "substrate", label: "Prev layer · IR", read: m => ({ value: unitHtml(fmt(m.substrate, 1), "°C"), sub: "substrate" }) },
  { id: "flow", label: "Extrusion ×", read: m => ({ value: m.flow_pct !== null ? unitHtml(fmt(m.flow_pct, 0), "%") : "unknown",
    sub: "multiplier readback" }) },
  { id: "speed", label: "Speed override", read: m => ({ value: unitHtml(fmt(m.speed_override, 0), "%"), sub: "" }) },
  { id: "vel", label: "Velocity", read: m => ({ value: unitHtml(fmt(m.vel, 1), "mm/s"), sub: m.moving ? "moving" : "still" }) },
  { id: "espeed", label: "Extr. speed", read: m => ({ value: fmt(m.extrusion_speed, 2), sub: "reported, no unit" }) },
  { id: "z", label: "Z", read: m => ({ value: m.pos ? unitHtml(fmt(m.pos.z, 2), "mm") : "—", sub: m.extruding ? "extruding" : "not extruding" }) },
  { id: "cool", label: "Cooling", read: m => ({ value: m.cooling_enabled === false ? "PID off" : unitHtml(fmt(m.cooling, 0), "%"),
    sub: `fan ${fmt(m.fan0, 0)} % PWM` }) },
];
$("#bridge").innerHTML = BRIDGE.map(b => `
  <div class="readout" id="r-${b.id}"><span class="silk">${b.label}</span><span class="value">—</span><span class="sub"></span></div>`).join("");

// ---- channel strips: fader = value to send, meter = printer readback --------------------------------------------
// Ranges from LEAM's command guide (20 Oct). Scale marks only fill a value; SET sends it.
const CONTROLS = [
  { op: "flow_percent", name: "Flow", title: "Extrusion multiplier", unit: "%", min: 0, max: 200, step: 1, value: 100,
    scale: [0, 60, 80, 100, 120, 140, 200] },
  { op: "pressure_advance_seconds", name: "PA", title: "Pressure advance", unit: "s", min: 0, max: 1, step: 0.005, value: 0,
    scale: [0, 0.1, 0.3, 0.6, 1] },
  { op: "nozzle_temperature_c", name: "Nozzle", title: "Nozzle target temperature", unit: "°C", min: 190, max: 250, step: 1,
    value: 235, scale: [190, 200, 220, 235, 250] },
  { op: "speed_override_percent", name: "Speed", title: "Speed override (Motion PI must be off)", unit: "%", min: 10, max: 200,
    step: 1, value: 100, scale: [10, 50, 100, 150, 200] },
  { op: "fan_speed_percent", name: "Fans", title: "Part-cooling fans (Cooling PI must be off)", unit: "%", min: 0, max: 100,
    step: 1, value: 0, scale: [0, 25, 50, 75, 100] },
];
// Printer readback per strip: the meter and the "now" value. Fan telemetry is PWM, which LEAM says can differ from the
// requested percentage, so fans get a meter but the fader doesn't follow it. PA has no readback at all.
const READBACK = {
  flow_percent: m => m.flow_pct,
  nozzle_temperature_c: m => m.nozzle?.target ?? null,
  speed_override_percent: m => m.speed_override ?? null,
  fan_speed_percent: m => m.fan0 ?? null,
};
const FOLLOWS = new Set(["flow_percent", "nozzle_temperature_c", "speed_override_percent"]);
const FOLLOW_AFTER_SET_MS = 5000;
const frac = (c, v) => clamp((v - c.min) / (c.max - c.min), 0, 1);

$("#strips").innerHTML = CONTROLS.map(c => `
  <div class="strip" data-op="${c.op}">
    <div class="strip-name" title="${c.title}"><span class="silk">${c.name}</span><span class="unit">${c.unit}</span></div>
    <div class="strip-now" title="Printer readback"><span class="now">—</span></div>
    <div class="fader-bay">
      <div class="scale">${c.scale.map(v => `<button type="button" tabindex="-1" data-v="${v}" style="--f:${frac(c, v)}"
        title="Fill ${v} ${c.unit}">${v}</button>`).join("")}</div>
      <input class="fader" type="range" min="${c.min}" max="${c.max}" step="${c.step}" value="${c.value}"
        aria-label="${c.title}, value to send">
      <div class="meter" aria-hidden="true" data-empty="1"><div class="meter-fill"></div></div>
    </div>
    <input class="lcd" type="number" min="${c.min}" max="${c.max}" step="${c.step}" value="${c.value}"
      aria-label="${c.title}, exact value">
    <button class="cue" type="button" title="Send to the printer">Set</button>
    <div class="pin"></div>
    <div class="outcome" aria-live="polite"></div>
  </div>`).join("");

document.querySelectorAll(".strip").forEach(el => {
  const [fader, lcd] = [el.querySelector(".fader"), el.querySelector(".lcd")];
  const edit = v => { fader.value = lcd.value = v; el.dataset.edited = "1"; };
  fader.oninput = () => edit(fader.value);
  lcd.oninput = () => { fader.value = lcd.value; el.dataset.edited = "1"; };
  lcd.onkeydown = e => { if (e.key === "Enter") el.querySelector(".cue").click(); };
  el.querySelectorAll(".scale button").forEach(b => (b.onclick = () => edit(b.dataset.v)));
  el.querySelector(".cue").onclick = async () => {
    if ((await send(el.dataset.op, lcd.value)).ok) {
      delete el.dataset.edited;
      el.dataset.followAt = Date.now() + FOLLOW_AFTER_SET_MS;
    }
  };
});

function renderStrips(m) {
  for (const c of CONTROLS) {
    const el = $(`.strip[data-op="${c.op}"]`);
    const live = m ? READBACK[c.op]?.(m) : null;
    const known = typeof live === "number";
    const meter = el.querySelector(".meter");
    meter.dataset.empty = known ? "" : "1";
    if (known) meter.style.setProperty("--f", frac(c, live));
    const out = known && (live < c.min || live > c.max);
    el.dataset.out = out ? "1" : "";
    el.querySelector(".now").textContent = !m ? "—" : !(c.op in READBACK) ? "n/a"
      : known ? `${fmt(live, c.step < 1 ? 2 : 0)}${c.op === "fan_speed_percent" ? " PWM" : ""}${out ? " ⚠" : ""}` : "—";
    el.querySelector(".strip-now").title = !(c.op in READBACK) ? "No readback in telemetry"
      : out ? "Printer value is outside the range LEAM allows us to send" : "Printer readback";
    if (known && FOLLOWS.has(c.op) && !el.dataset.edited && Date.now() >= (+el.dataset.followAt || 0)) {
      el.querySelector(".fader").value = el.querySelector(".lcd").value = live;
    }
  }
}

// ---- telemetry + staleness ---------------------------------------------------------------------------------------
let lastMetrics = null;
function renderTelemetry(m, ageMs = 0) {
  lastMetrics = m;
  lastTelemetryAt = Date.now() - ageMs;
  $("#bridge").classList.remove("stale");
  for (const b of BRIDGE) {
    const r = b.read(m), el = $(`#r-${b.id}`);
    el.querySelector(".value").innerHTML = r.value;
    el.querySelector(".sub").textContent = r.sub;
    el.dataset.warn = r.warn ? "1" : "";
  }
  led($("#st-gateway"), `gateway ${m.gateway ?? "?"}`, m.gateway === "RUN" ? "ok" : "warn");
  $("#program").textContent = m.program || "";
  renderStrips(m);
}

setInterval(() => {
  if (Date.now() - lastTelemetryAt <= STALE_MS || $("#bridge").classList.contains("stale")) return;
  $("#bridge").classList.add("stale");
  document.querySelectorAll("#bridge .readout .value").forEach(v => (v.textContent = "—"));
  document.querySelectorAll("#bridge .readout .sub").forEach(v => (v.textContent = "no data"));
  led($("#st-gateway"), "gateway no data", "bad");
  $("#program").textContent = "";
  renderStrips(null);
}, 250);

// ---- commands, outcomes, log -------------------------------------------------------------------------------------
const opByRequest = new Map();
const OUTCOME = {
  accepted: ["pending", "accepted"], setting_confirmed: ["ok", "✓"], rejected: ["bad", "✕ rejected"],
  queued_unconfirmed: ["warn", "? queued"], dispatch_unknown: ["warn", "? unknown"],
};

function setOutcome(op, state, text, title) {
  const o = $(`.strip[data-op="${op}"] .outcome`);
  if (!o) return;
  o.dataset.state = state;
  o.textContent = text;
  o.title = title || text;
}

function trackOutcome(e) {
  const p = e.payload || {};
  if (e.kind === "request" && p.correction) {
    opByRequest.set(p.request_id, p.correction.operation);
    setOutcome(p.correction.operation, "pending", `sent ${p.correction.value}`);
  } else if (e.kind === "refused" && p.request && p.request.operation) {
    setOutcome(p.request.operation, "bad", "✕ not sent", p.error);
  } else if (e.kind === "result" && opByRequest.has(p.request_id)) {
    const [state, text] = OUTCOME[p.state] || ["warn", p.state];
    const observed = p.state === "setting_confirmed" ? ` ${p.observation?.observed_value ?? ""}` : "";
    setOutcome(opByRequest.get(p.request_id), state, text + observed, p.reason || p.state);
  }
}

function describe(e) {
  const p = e.payload || {};
  if (e.kind === "refused") {
    const r = p.request;
    return `✕ not sent: ${r && r.operation ? `${r.operation} = ${r.value}` : "raw"} · ${p.error}`;
  }
  if (e.kind === "request") {
    const c = p.correction;
    return c ? `→ ${c.operation} = ${c.value} · seq ${p.sequence}` : `→ raw ${JSON.stringify(p)}`;
  }
  const obs = p.observation ? ` · observed ${p.observation.observed_value}` : "";
  return `← ${p.state}${p.reason ? ` (${p.reason})` : ""}${obs} · seq ${p.sequence ?? "?"}${e.ours ? "" : " · other sender"}`;
}

function logLine(e) {
  trackOutcome(e);
  const li = document.createElement("li");
  li.className = e.kind;
  if (e.kind === "result") li.dataset.state = (OUTCOME[e.payload?.state] || ["warn"])[0];
  li.title = JSON.stringify(e.payload);
  li.innerHTML = `<time>${new Date(e.t).toLocaleTimeString()}</time><span></span>`;
  li.lastChild.textContent = describe(e);
  $("#log").prepend(li);
  while ($("#log").children.length > 60) $("#log").lastChild.remove();
}

let writerStatusAt = 0;
function renderWriterStatus(s, live = true) {
  if (!s) return;
  if (live) writerStatusAt = Date.now();  // a snapshot's status has unknown age, so it never counts as fresh
  $("#writer-status").textContent = `session ${String(s.gateway_session_id).slice(0, 8)} · seq ${s.highest_admitted_sequence}`;
  renderWriteLed(s);
}
let lastWriterStatus = null, writerConn = "off";
function renderWriteLed(s = lastWriterStatus) {
  lastWriterStatus = s;
  const el = $("#write-led");
  if (writerConn !== "connected") return led(el, "write off", "off");
  if (!s || Date.now() - writerStatusAt > 3000) return led(el, "write · gate status unknown", "warn");
  if (s.normal_authorized !== true) return led(el, "write locked · gate closed", "warn");
  led(el, "write armed · gate open", "ok");
}
setInterval(() => renderWriteLed(), 1000);
on("snapshot", m => { writerConn = m.status.writer; renderWriteLed(); });
on("status", s => { writerConn = s.writer; renderWriteLed(); });

const post = async body => { try { return await api("api/request", body); } catch (e) { return { ok: false, error: "request failed" }; } };

async function send(operation, value) {
  const r = await post({ operation, value: Number(value) });
  $("#send-msg").textContent = r.ok ? `Sent ${operation} = ${value}, waiting for the result` : `Not sent: ${r.error}`;
  $("#send-msg").dataset.state = r.ok ? "ok" : "bad";
  return r;
}

async function fillTemplate() {
  $("#raw").value = JSON.stringify(await api("api/request-template"), null, 2);
}
$("#raw-template").onclick = fillTemplate;
$("#raw-send").onclick = async () => {
  const r = await post({ raw: $("#raw").value });
  $("#send-msg").textContent = r.ok ? "Raw request sent, waiting for the result" : `Not sent: ${r.error}`;
  $("#send-msg").dataset.state = r.ok ? "ok" : "bad";
};

on("snapshot", async m => {
  Object.values(m.frames || {}).forEach(onFrame);
  const last = m.history[m.history.length - 1];
  if (last && m.server_ms - last.t <= STALE_MS) renderTelemetry(last, m.server_ms - last.t);
  $("#log").innerHTML = "";
  m.writer_log.slice().reverse().forEach(logLine);
  $("#req-topic").textContent = m.request_topic;
  if (!$("#raw").value) await fillTemplate();
  renderWriterStatus(m.writer_status, false);
});
on("frame", onFrame);
on("telemetry", m => renderTelemetry(m));
on("writer_log", logLine);
on("writer_status", m => renderWriterStatus(m.payload));

// ---- width controller: estimate, target, Auto switch, decisions, pins ---------------------------------------------
const ESTIMATE_STALE_MS = 5000;  // an estimate older than this is blanked, like any other readout
const DECISION_STALE_MS = 5000;  // judged by the server-stamped age, so a stopped control loop shows within seconds
const DECISION_TRACKED_STALE_MS = 15000;  // server without age stamps: the decision's t_ms must keep changing
const LINK_STALE_MS = 5000;  // no successful poll for this long: the controller section is treated as offline
const MSG_MS = 8000;
const CONF_OK = 0.7, CONF_WARN = 0.4;
const isNum = v => typeof v === "number" && Number.isFinite(v);
const signed = (v, digits = 2) => {
  const s = Math.abs(v).toFixed(digits);
  return +s === 0 ? s : (v < 0 ? "−" : "+") + s;
};
const bound = (v, digits = 2) => {  // a lower bound always carries its sign, so "≥ +0.00" never reads as "on target"
  const s = Math.abs(v).toFixed(digits);
  return (v < 0 && +s !== 0 ? "−" : "+") + s;
};
const OP_NAME = Object.fromEntries(CONTROLS.map(c => [c.op, c]));
const digitsOf = op => (OP_NAME[op]?.step < 1 ? 2 : 0);
const opName = op => OP_NAME[op]?.name.toLowerCase() ?? op;
const move = (op, from, to) =>
  `${opName(op)} ${isNum(from) ? `${fmt(from, digitsOf(op))} → ` : "→ "}${fmt(to, digitsOf(op))}${OP_NAME[op] ? ` ${OP_NAME[op].unit}` : ""}`;
const node = (tag, className = "", text = "") => Object.assign(document.createElement(tag), { className, textContent: text });

let control = null;  // last good state; kept while the link is down, never shown as live then
let lastGoodAt = 0, pollFailed = false, serverOffset = null;  // serverOffset: server clock minus browser clock
const startedAt = Date.now();
let estimate = null;  // {mm, sigma_mm, n, confidence, at}: `at` is when it was measured, in browser time
let decision = null;  // {d, at, tracked}: `at` is when it was made, in browser time
let pinned = {};

const linkOk = () => !pollFailed && lastGoodAt > 0 && Date.now() - lastGoodAt <= LINK_STALE_MS;
const serverNowMs = () => (serverOffset !== null ? Date.now() + serverOffset
  : decision && isNum(decision.d.t_ms) ? decision.d.t_ms + (Date.now() - decision.at) : Date.now());
const decisionLive = () => decision && Date.now() - decision.at <= (decision.tracked ? DECISION_TRACKED_STALE_MS : DECISION_STALE_MS);

// ---- target and Hold
const targetEl = $("#target");
let targetEdited = false, targetBusy = false, targetMsgTimer = 0, targetMsgClearOnPoll = false;

function showTargetMsg(text, clearOnPoll = false, state = "bad") {
  const sub = $("#target-sub");
  clearTimeout(targetMsgTimer);
  targetMsgClearOnPoll = clearOnPoll && !!text;
  sub.textContent = text;
  sub.title = text;
  sub.dataset.state = text ? state : "";
  if (text) targetMsgTimer = setTimeout(() => showTargetMsg(""), MSG_MS);
  else renderTarget();
}
const targetMsgOf = r => (r.lost ? r.error : r.error ? `Not set: ${r.error}` : "");

function renderTarget(force = false) {
  const up = linkOk(), rel = control?.relative === true;
  targetEl.disabled = !up || rel;
  $("#hold").disabled = !up;
  targetEl.dataset.offline = up ? "" : "1";
  targetEl.dataset.relative = up && rel ? "1" : "";
  targetEl.title = up && rel ? "Relative width: a typed target has no meaning, only Hold" : "";
  if (!control) return;
  if (rel) targetEdited = false;
  const t = control.target_mm;
  if (force || (!targetEdited && !targetBusy)) targetEl.value = isNum(t) ? +t.toFixed(3) : "";  // busy: an older poll mustn't undo the post
  if (!$("#target-sub").dataset.state) {
    const d = isNum(control.limits?.band_default_mm) ? control.limits.band_default_mm : 0.05;
    const below = isNum(control.band_below_mm) ? control.band_below_mm : d, above = isNum(control.band_above_mm) ? control.band_above_mm : d;
    $("#target-sub").textContent = !up ? "controller offline" : [isNum(t) ? `zone ${fmt(t - below, 2)}–${fmt(t + above, 2)}` : "no target",
      rel ? "relative" : "", isNum(control.ceiling_mm) ? `max ${fmt(control.ceiling_mm, 2)}` : ""].filter(Boolean).join(" · ");
  }
}

async function commitTarget() {
  if (!targetEdited) return;
  targetEdited = false;
  if (targetEl.validity.badInput) {  // the field's value reads "" then, which would silently clear the target
    renderTarget(true);
    return showTargetMsg("Not set: not a number");
  }
  const raw = targetEl.value.trim();
  targetBusy = true;
  const want = raw === "" ? null : Number(raw);
  const r = await postControl({ target_mm: want });
  targetBusy = false;
  showTargetMsg(targetMsgOf(r), r.lost && control?.target_mm === want);
  if (r.error) renderTarget(true);
  else if (!targetEdited) targetEl.blur();
}
targetEl.oninput = () => (targetEdited = true);
targetEl.onblur = commitTarget;
targetEl.onkeydown = e => {
  if (e.key === "Enter") commitTarget();
  else if (e.key === "Escape") { targetEdited = false; renderTarget(true); targetEl.blur(); }
};
$("#hold").onmousedown = () => (targetEdited = false);  // Hold replaces a half-typed value instead of posting it first
$("#hold").onclick = async () => {
  targetEdited = false;
  targetBusy = true;
  const before = control?.target_mm, seen = liveEstimate();
  const r = await postControl({ hold_current: true });
  targetBusy = false;
  showTargetMsg(targetMsgOf(r), r.lost && isNum(control?.target_mm) && control.target_mm !== before);
  if (!r.error && seen?.at_ceiling === true) {
    showTargetMsg(`width is at the camera's ceiling; holding ≥ ${fmt(isNum(r.target_mm) ? r.target_mm : floorOf(seen), 2)} mm`, false, "warn");
  }
  if (r.error) renderTarget(true);
};

// ---- width, difference
const liveEstimate = () => (estimate && Date.now() - estimate.at <= ESTIMATE_STALE_MS ? estimate : null);
// The camera is blind above its ceiling: the width is then a lower bound, never more than what was read.
const floorOf = e => Math.min(e.mm, isNum(e.ceiling_mm) ? e.ceiling_mm : isNum(control?.ceiling_mm) ? control.ceiling_mm : e.mm);

function renderWidth() {
  const live = liveEstimate();
  const w = $("#r-width"), d = $("#r-delta"), conf = $("#conf-led"), rel = control?.relative === true;
  w.querySelector(".silk").textContent = rel ? "Width · rel." : "Width";
  conf.hidden = !live;
  const capped = !!live && live.at_ceiling === true;
  if (!live) {
    w.querySelector(".value").textContent = "—";
    $("#width-note").textContent = estimate ? "estimate stale" : "no estimate";
    w.title = "";
  } else {
    const sigma = isNum(live.sigma_mm) ? `± ${fmt(live.sigma_mm, 2)} mm` : "mm";
    w.querySelector(".value").innerHTML = capped ? `≥ ${fmt(floorOf(live), 2)}<small>mm</small>` : `${fmt(live.mm, 2)}<small>${sigma}</small>`;
    const c = live.confidence;
    led(conf, `conf ${fmt(c, 2)}`, !isNum(c) ? "off" : c >= CONF_OK ? "ok" : c >= CONF_WARN ? "warn" : "bad");
    $("#width-note").textContent = isNum(live.n) ? `n ${live.n}` : "";
    w.title = [capped ? `The camera can't see beads wider than ${fmt(floorOf(live), 2)} mm: the real width is at least this` : "",
      isNum(live.model_sigma_mm) ? `calibration ± ${fmt(live.model_sigma_mm, 2)} mm` : "",
      rel ? "relative scale, not calibrated to millimetres" : ""].filter(Boolean).join("\n");
  }
  const t = linkOk() ? control.target_mm : null;
  const diff = live && isNum(t) ? (capped ? floorOf(live) : live.mm) - t : null;
  d.querySelector(".value").innerHTML = diff === null ? "—" : capped ? `≥ ${bound(diff)}<small>mm</small>` : `${signed(diff)}<small>mm</small>`;
  d.querySelector(".sub").textContent = diff === null ? "" : capped ? `≥ ${bound((diff / t) * 100, 1)} %` : `${signed((diff / t) * 100, 1)} %`;
  d.title = diff !== null && capped ? "At least this much: the camera can't see wider" : "";
}

function setEstimate(e) {
  estimate = e && isNum(e.mm) ? { ...e, at: Date.now() - (isNum(e.age_ms) ? e.age_ms : 0) } : null;
  renderWidth();
}

// ---- decisions
// ageMs: how old the server says the decision is; null when it can't say, then liveness follows t_ms changing.
function setDecision(d, ageMs) {
  if (!d) decision = null;
  else if (!decision || d.t_ms !== decision.d.t_ms) {
    const tracked = !isNum(ageMs), at = Date.now() - (tracked ? 0 : Math.max(0, ageMs));
    if (decision && !tracked && !decision.tracked && at <= decision.at) return;  // a poll answered after a newer socket message
    decision = { d, at, tracked };
  }
  renderDecision();
}

const chosenOf = d => d.action && (d.candidates || []).find(c => c.operation === d.action.operation && c.value === d.action.value);

// The controller leads a reason with this alarm while it keeps acting on the rest; it shows as its own line above the decision.
const NARROW_ALARM = /^alarm:\s*(below [\d.]+ mm)\s*\(walls may not touch\)\s*;?\s*/;

function decisionLine(d) {
  const raw = d.reason || "", narrowMatch = NARROW_ALARM.exec(raw);
  const narrow = narrowMatch ? `${narrowMatch[1]}: walls may not touch` : "";
  const rest = narrowMatch ? raw.slice(narrowMatch[0].length) : raw;  // a latched alarm behind it stays an alarm
  const alarm = /^alarm:/.test(rest);
  const floorRules = /^floor rules:\s*/.test(rest);  // acted under the floor rules (D48), also in the 1.30-1.35 mm zone
  const reason = rest.replace(/^alarm:\s*/, "").replace(/^floor rules:\s*/, "");
  const a = d.action;
  const act = a ? move(a.operation, chosenOf(d)?.from, a.value) : "";
  if (a && d.sent === false) {  // refused at the last moment: the action did not happen, so it must not read like it did
    return { text: `not sent · ${reason.replace(/^not sent:\s*/, "") || "refused"}`, alarm, narrow, notSent: true, intended: act };
  }
  const parts = alarm ? ["alarm", reason] : a ? [act, floorRules ? "floor rules" : "", d.hold ? "hold" : "", d.hold ? reason : ""] : [d.hold ? "hold" : "", reason || "no action"];
  return { text: parts.filter(Boolean).join(" · "), alarm, narrow, notSent: false, intended: "" };
}

// "±10", or "±10 (±20 below 1.30 mm)" when the floor rules may take bigger steps (D48)
function stepText(l, limits) {
  const f = limits?.floor_step_factor, floor = isNum(f) && f > 1 && isNum(limits.min_width_mm);
  return `±${l.max_step}${floor ? ` (±${+(l.max_step * f).toFixed(2)} below ${fmt(limits.min_width_mm, 2)} mm)` : ""}`;
}

function limitsLine(limits) {
  const levers = limits?.levers;
  if (!levers) return "";
  const each = Object.entries(levers).map(([op, l]) => `${opName(op)} ${l.min}–${l.max} ${stepText(l, limits)}`);
  return `${limits.mode ? `${limits.mode} mode · ` : ""}limits ${each.join(" · ")}`;
}

function whyBody(d) {
  const body = $("#why-body");
  body.replaceChildren();
  const line = text => text && body.append(node("div", "why-line", text));
  line([isNum(d.error_mm) ? `needs ${signed(d.error_mm)} mm` : "", isNum(d.predicted_mm) ? `predicted ${fmt(d.predicted_mm, 2)} mm` : ""]
    .filter(Boolean).join(" · "));
  line(d.reason ? `reason ${d.reason}` : "");
  line(limitsLine(control?.limits));
  const cands = Array.isArray(d.candidates) ? d.candidates : [];
  if (!cands.length) return;
  const ul = node("ul", "cands");
  for (const cand of cands) {
    const chosen = chosenOf(d) === cand;
    const li = node("li");
    li.dataset.chosen = chosen ? "1" : "";
    li.append(node("span", "", chosen ? "▸" : ""), node("span", "", move(cand.operation, cand.from, cand.value)),
      node("span", "num", isNum(cand.score) ? `score ${fmt(cand.score, 2)}` : ""), node("span", "num", chosen ? "chosen" : ""));
    if (isNum(cand.predicted_change_mm)) li.append(node("span", "more", `predicted change ${signed(cand.predicted_change_mm)} mm`));
    ul.append(li);
  }
  body.append(ul);
}

let shownWhy = "";
function renderDecision() {
  const box = $("#decision"), why = $("#why");
  if (!decisionLive()) {
    box.replaceChildren();
    box.dataset.sig = box.dataset.alarm = box.dataset.mode = box.dataset.sent = box.title = "";
    why.hidden = true;
    why.open = false;
    shownWhy = "";
    return;
  }
  const d = decision.d, { text, alarm, narrow, notSent, intended } = decisionLine(d);
  const auto = typeof d.mode === "string" && d.mode.startsWith("auto"), dry = auto && (d.dry === true || (control && !control.send_enabled));
  const chip = !auto ? "advice" : dry ? "auto · dry" : "auto";
  if (box.dataset.sig !== `${chip}|${narrow}|${text}|${intended}`) {
    box.dataset.sig = `${chip}|${narrow}|${text}|${intended}`;
    box.replaceChildren(...(narrow ? [node("span", "alarm-line", narrow)] : []), node("span", "mode", chip),
      node("span", notSent ? "unsent" : "", text), ...(intended ? [node("span", "intended", intended)] : []));
    box.title = `${chip}: ${d.reason || text}`;
  }
  box.dataset.sent = notSent ? "0" : "";
  box.dataset.alarm = alarm ? "1" : "";
  box.dataset.mode = auto ? "auto" : "advise";
  box.dataset.dry = dry ? "1" : "";
  why.hidden = false;
  const sig = JSON.stringify({ ...d, t_ms: 0, age_ms: 0, type: 0, limits: control?.limits });
  if (sig !== shownWhy) { shownWhy = sig; whyBody(d); }  // the poll and the socket both repeat unchanged decisions
}

// ---- Auto switch, link, LEDs
let ctlMsgTimer = 0, ctlMsgClearOnPoll = false;
function showCtlMsg(text, clearOnPoll = false) {
  const m = $("#ctl-msg");
  clearTimeout(ctlMsgTimer);
  ctlMsgClearOnPoll = clearOnPoll && !!text;
  m.textContent = text;
  m.dataset.state = text ? "bad" : "";
  if (text) ctlMsgTimer = setTimeout(() => showCtlMsg(""), MSG_MS);
}

function renderAuto() {
  const btn = $("#auto"), l = $("#auto-led"), s = $("#send-led"), up = linkOk();
  $("#ctl").dataset.link = up ? "ok" : "offline";
  btn.disabled = !up;
  btn.setAttribute("aria-busy", btn.dataset.busy === "1");
  btn.setAttribute("aria-checked", up && control.auto === true ? "true" : "false");
  if (!up) {
    const waiting = !control && !pollFailed && Date.now() - startedAt <= LINK_STALE_MS;
    led(l, waiting ? "—" : "controller offline", waiting ? "off" : "bad");
    led(s, "sends ?", "off");
    return;
  }
  if (!control.auto) led(l, "advice", "off");
  else {
    const dry = !control.send_enabled, quiet = !decisionLive(), alarm = !quiet && /^alarm:/.test(decision.d.reason || "");
    led(l, ["auto", dry ? "dry" : "", quiet ? "no decisions" : "", alarm ? "alarm" : ""].filter(Boolean).join(" · "), dry || quiet || alarm ? "warn" : "ok");
    l.title = [dry ? "Auto is on, but this app is not set to send commands to the printer" : "",
      quiet ? "No fresh decision from the control loop" : "", alarm ? decision.d.reason : ""].filter(Boolean).join("\n") || "Auto is on";
  }
  led(s, control.send_enabled ? "sends on" : "sends off", control.send_enabled ? "ok" : "off");
  s.title = control.send_enabled ? "Commands from Auto are sent to the printer" : "Commands are not sent: only advice and dry runs";
}

function renderModels(models) {
  const lamp = $("#st-model"), names = Object.keys(models || {});
  if (!names.length || !linkOk()) return led(lamp, "models ?", "off");
  const have = names.filter(n => models[n].present);
  led(lamp, have.length === names.length ? "models ok" : have.length ? `models ${have.length}/${names.length}` : "models missing",
    have.length === names.length ? "ok" : have.length ? "warn" : "bad");
  lamp.title = names.map(n => `${n.replace(/_/g, " ")}: ${models[n].present ? `present${models[n].version ? ` v${models[n].version}` : ""}`
    : `missing${models[n].error ? ` (${models[n].error})` : ""}`}`).join("\n");
}

function renderFiles() {
  const issues = [control?.persist_error ? `target not saved: ${control.persist_error}` : "",
    control?.load_error ? `saved settings ignored: ${control.load_error}` : ""].filter(Boolean);
  $("#ctl-warn").textContent = linkOk() ? issues.join(" · ") : "";
  $("#ctl-warn").dataset.state = "bad";
}

// ---- pins and limits on the strips
function renderLevers() {
  const levers = control?.limits?.levers || {};
  const left = p => (isNum(p.until_ms) ? (p.until_ms - serverNowMs()) / 1000 : null);
  for (const c of CONTROLS) {
    const strip = $(`.strip[data-op="${c.op}"]`), pin = strip.querySelector(".pin"), p = linkOk() ? pinned[c.op] : null;
    const l = levers[c.op], secs = p ? left(p) : null;
    const show = p && (serverOffset === null || secs === null || secs > 0);
    strip.dataset.pinned = show ? "1" : "";
    pin.textContent = show ? "pinned" : "";
    pin.title = !show ? "" : `Set from outside${isNum(p.value) ? ` to ${fmt(p.value, digitsOf(c.op))}` : ""}; the controller leaves it alone${
      secs > 0 ? ` for ${Math.floor(secs / 60)}:${String(Math.floor(secs % 60)).padStart(2, "0")} more` : ""}`;
    strip.querySelector(".strip-name").title = l ? `${c.title}\nAuto keeps it within ${l.min}–${l.max}, at most ${stepText(l, control.limits)} per command` : c.title;
  }
}

function renderAll() {
  renderAuto();
  renderTarget();
  renderModels(control?.models);
  renderFiles();
  renderWidth();
  renderDecision();
  renderLevers();
}

on("control", c => {
  if (c.offline) {
    const wasUp = linkOk();
    pollFailed = true;
    if (wasUp) targetEdited = false;  // an edit can't be committed now; show the last known value instead
    return renderAll();
  }
  pollFailed = false;
  lastGoodAt = Date.now();
  serverOffset = isNum(c.server_ms) ? c.server_ms - Date.now() : null;
  control = c;
  pinned = c.pinned || {};
  if (ctlMsgClearOnPoll) showCtlMsg("");
  if (targetMsgClearOnPoll) showTargetMsg("");
  if (!c.estimate) estimate = null;
  else if (!estimate || Date.now() - (c.estimate.age_ms || 0) > estimate.at) setEstimate(c.estimate);
  const d = c.last_decision;
  setDecision(d, d && (isNum(d.age_ms) ? d.age_ms : isNum(c.server_ms) && isNum(d.t_ms) ? c.server_ms - d.t_ms : null));
  renderAll();
});
on("width", m => setEstimate(m));
on("decision", m => { setDecision(m, isNum(m.age_ms) ? m.age_ms : 0); renderAuto(); });
setInterval(renderAll, 500);

$("#auto").onclick = async () => {
  const btn = $("#auto");
  if (!linkOk() || btn.dataset.busy === "1") return;
  btn.dataset.busy = "1";
  renderAuto();
  const want = !control.auto;
  const r = await postControl({ auto: want });
  btn.dataset.busy = "";
  showCtlMsg(r.lost ? r.error : r.error ? `Not set: ${r.error}` : "", r.lost && control.auto === want);
  renderAuto();
};
renderAll();

setupSettings();
connectSocket();
startControlPoll();
