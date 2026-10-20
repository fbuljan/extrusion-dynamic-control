const showIr = cameraView("#ir", "#ir-label", "#ir-empty", "IR");
const showRgb = cameraView("#rgb", "#rgb-label", "#rgb-empty", "RGB");

function onFrame(f) { (f.camera_kind === "ir" ? showIr : showRgb)(f); }

const STALE_MS = 1000;  // guide: telemetry expires 1 s after publish, and nothing may be shown past that
let lastTelemetryAt = 0;

const heaterText = h => !h ? "—"
  : `${fmt(h.current, 1)} / ${fmt(h.target, 0, " °C")}` + (h.state !== "active" ? ` <small>${h.state}</small>` : "");

function renderTelemetry(m, ageMs = 0) {
  lastTelemetryAt = Date.now() - ageMs;
  $(".metrics").classList.remove("stale");
  $("#m-nozzle").innerHTML = heaterText(m.nozzle);
  $("#m-bed").innerHTML = heaterText(m.bed);
  $("#m-melt").innerHTML = m.melt !== null ? fmt(m.melt, 1, " °C") : `— <small>${m.melt_reason || ""}</small>`;
  $("#m-substrate").textContent = fmt(m.substrate, 1, " °C");
  $("#m-flow").textContent = m.flow_pct !== null ? fmt(m.flow_pct, 0, " %") : "unknown";
  $("#m-override").textContent = fmt(m.speed_override, 0, " %");
  $("#m-vel").textContent = fmt(m.vel, 1, " mm/s");
  $("#m-espeed").textContent = fmt(m.extrusion_speed, 2);
  $("#m-z").textContent = m.pos ? fmt(m.pos.z, 2, " mm") : "—";
  $("#m-cool").textContent = `${m.cooling_enabled === false ? "PID off" : fmt(m.cooling, 0, "%")} / ${fmt(m.fan0, 0, "%")}`;
  pill($("#st-gateway"), `gateway: ${m.gateway ?? "?"}`, m.gateway === "RUN" ? "ok" : "warn");
  $("#program").textContent = m.program || "";
  document.querySelectorAll(".control[data-op]").forEach(el => {
    const now = NOW[el.dataset.op];
    el.querySelector(".now").textContent = now ? `now ${now(m)}` : "now: no readback";
    const live = LIVE[el.dataset.op]?.(m);
    if (typeof live === "number" && !el.dataset.edited && Date.now() >= (+el.dataset.followAt || 0)) {
      el.querySelectorAll("input").forEach(i => (i.value = live));
    }
  });
}

// Exact readbacks only: fan telemetry is PWM, which LEAM says can differ from the requested percentage.
const LIVE = {
  flow_percent: m => m.flow_pct,
  nozzle_temperature_c: m => m.nozzle?.target,
  speed_override_percent: m => m.speed_override,
};

// The printer's current value per control, so a refreshed page shows reality rather than the input defaults.
const NOW = {
  flow_percent: m => (m.flow_pct !== null ? fmt(m.flow_pct, 0, " %") : "unknown"),
  nozzle_temperature_c: m => (m.nozzle ? fmt(m.nozzle.target, 0, " °C") : "—"),
  speed_override_percent: m => fmt(m.speed_override, 0, " %"),
  fan_speed_percent: m => fmt(m.fan0, 0, " % PWM"),
};

setInterval(() => {
  if (Date.now() - lastTelemetryAt <= STALE_MS || $(".metrics").classList.contains("stale")) return;
  $(".metrics").classList.add("stale");
  document.querySelectorAll(".metric .v, .control .now").forEach(v => (v.textContent = "—"));
  pill($("#st-gateway"), "gateway: no data", "bad");
  $("#program").textContent = "";
}, 250);

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
  const li = document.createElement("li");
  li.className = e.kind;
  li.title = JSON.stringify(e.payload);
  li.textContent = `${new Date(e.t).toLocaleTimeString()} ${describe(e)}`;
  $("#log").prepend(li);
  while ($("#log").children.length > 60) $("#log").lastChild.remove();
}

function renderWriterStatus(s) {
  if (!s) return;
  const gate = s.normal_authorized === true ? "gate open (RUN)" : "gate closed";
  $("#writer-status").textContent =
    `${gate} · session ${String(s.gateway_session_id).slice(0, 8)} · last admitted seq ${s.highest_admitted_sequence}`;
}

async function send(operation, value) {
  const r = await api("api/request", { operation, value: Number(value) });
  $("#send-msg").textContent = r.ok ? `sent ${operation} = ${value}, waiting for result` : `not sent: ${r.error}`;
  return r;
}

// Ranges from LEAM's command guide (20 Oct). Presets only fill the inputs; Set sends.
const CONTROLS = [
  { op: "flow_percent", name: "Flow", hint: "extrusion multiplier, 0–200 %", min: 0, max: 200, step: 1, value: 100,
    presets: [[60, "60 under"], [80], [100, "100 nominal"], [120], [140, "140 over"]] },
  { op: "pressure_advance_seconds", name: "Pressure advance", hint: "0–1 s", min: 0, max: 1, step: 0.005, value: 0,
    presets: [[0], [0.05], [0.1], [0.3], [0.6]] },
  { op: "nozzle_temperature_c", name: "Nozzle temperature", hint: "target, 190–250 °C", min: 190, max: 250, step: 1,
    value: 235, presets: [[200], [220], [235], [250]] },
  { op: "speed_override_percent", name: "Speed override", hint: "10–200 %, Motion PI off", min: 10, max: 200, step: 1,
    value: 100, presets: [[50], [60], [100], [150]] },
  { op: "fan_speed_percent", name: "Part-cooling fans", hint: "0–100 %, Cooling PI off", min: 0, max: 100, step: 1,
    value: 0, presets: [[0], [40], [100]] },
];
$("#controls").innerHTML = CONTROLS.map(c => `
  <div class="control" data-op="${c.op}">
    <div class="row"><strong>${c.name}</strong><span class="muted">${c.hint}</span><span class="now mono"></span></div>
    <div class="row">
      <input type="range" min="${c.min}" max="${c.max}" step="${c.step}" value="${c.value}">
      <input type="number" min="${c.min}" max="${c.max}" step="${c.step}" value="${c.value}">
      <button class="primary">Set</button>
    </div>
    <div class="presets">${c.presets.map(([v, label]) => `<button data-v="${v}">${label ?? v}</button>`).join("")}</div>
  </div>`).join("");
// Inputs follow the printer's live value until edited; after Set they follow it again once the readback can catch up.
const FOLLOW_AFTER_SET_MS = 5000;
document.querySelectorAll(".control[data-op]").forEach(el => {
  const [range, num] = el.querySelectorAll("input");
  range.oninput = () => { num.value = range.value; el.dataset.edited = "1"; };
  num.oninput = () => { range.value = num.value; el.dataset.edited = "1"; };
  el.querySelector("button.primary").onclick = async () => {
    if ((await send(el.dataset.op, num.value)).ok) {
      delete el.dataset.edited;
      el.dataset.followAt = Date.now() + FOLLOW_AFTER_SET_MS;
    }
  };
  el.querySelectorAll(".presets button").forEach(b => (b.onclick = () => {
    range.value = num.value = b.dataset.v;
    el.dataset.edited = "1";
  }));
});

async function fillTemplate() {
  $("#raw").value = JSON.stringify(await api("api/request-template"), null, 2);
}
$("#raw-template").onclick = fillTemplate;
$("#raw-send").onclick = async () => {
  const r = await api("api/request", { raw: $("#raw").value });
  $("#send-msg").textContent = r.ok ? "raw request sent, waiting for result" : `not sent: ${r.error}`;
};

on("snapshot", async m => {
  Object.values(m.frames || {}).forEach(onFrame);
  const last = m.history[m.history.length - 1];
  if (last && m.server_ms - last.t <= STALE_MS) renderTelemetry(last, m.server_ms - last.t);
  m.writer_log.slice().reverse().forEach(logLine);
  $("#req-topic").textContent = m.request_topic;
  if (!$("#raw").value) await fillTemplate();
  renderWriterStatus(m.writer_status);
});
on("frame", onFrame);
on("telemetry", m => renderTelemetry(m));
on("writer_log", logLine);
on("writer_status", m => renderWriterStatus(m.payload));

setupSettings();
connectSocket();
