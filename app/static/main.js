const showIr = cameraView("#ir", "#ir-label", "#ir-empty", "IR");
const showRgb = cameraView("#rgb", "#rgb-label", "#rgb-empty", "RGB");

function onFrame(f) { (f.camera_kind === "ir" ? showIr : showRgb)(f); }

function renderTelemetry(m) {
  $("#m-melt").innerHTML = m.melt !== null ? fmt(m.melt, 1, " °C") : `— <small>${m.melt_reason || ""}</small>`;
  $("#m-substrate").textContent = fmt(m.substrate, 1, " °C");
  $("#m-flow").textContent = m.flow_pct !== null ? fmt(m.flow_pct, 0, " %") : "unknown";
  $("#m-override").textContent = fmt(m.speed_override, 0, " %");
  $("#m-vel").textContent = fmt(m.vel, 1, " mm/s");
  $("#m-espeed").textContent = fmt(m.extrusion_speed, 2);
  $("#m-z").textContent = m.pos ? fmt(m.pos.z, 2, " mm") : "—";
  $("#m-cool").textContent = `${fmt(m.cooling, 0, "%")} / ${fmt(m.fan0, 0, "%")}`;
  pill($("#st-gateway"), `gateway: ${m.gateway ?? "?"}`, m.gateway === "RUN" ? "ok" : "warn");
  $("#program").textContent = m.program || "";
}

function logLine(e) {
  const li = document.createElement("li");
  li.className = e.kind;
  const time = new Date(e.t).toLocaleTimeString();
  li.textContent = `${time} ${e.kind === "request" ? "→" : "←"} ${JSON.stringify(e.payload)}`;
  $("#log").prepend(li);
  while ($("#log").children.length > 60) $("#log").lastChild.remove();
}

async function send(operation, value) {
  const r = await api("api/request", { operation, value: Number(value) });
  $("#send-msg").textContent = r.ok ? `sent ${operation} = ${value}` : `not sent: ${r.error}`;
}

function bindControl(rangeSel, numSel, sendSel, operation) {
  const range = $(rangeSel), num = $(numSel);
  range.oninput = () => (num.value = range.value);
  num.oninput = () => (range.value = num.value);
  $(sendSel).onclick = () => send(operation, num.value);
}
bindControl("#flow-range", "#flow-num", "#flow-send", "flow_percent");
bindControl("#pa-range", "#pa-num", "#pa-send", "pressure_advance_seconds");
const inputsFor = { flow_percent: ["#flow-range", "#flow-num"], pressure_advance_seconds: ["#pa-range", "#pa-num"] };
document.querySelectorAll(".presets").forEach(group => {
  group.querySelectorAll("button").forEach(b => (b.onclick = () => {
    inputsFor[group.dataset.op].forEach(sel => ($(sel).value = b.dataset.v));
    send(group.dataset.op, b.dataset.v);
  }));
});

$("#raw-send").onclick = async () => {
  const r = await api("api/request", { raw: $("#raw").value });
  $("#send-msg").textContent = r.ok ? "raw request sent" : `not sent: ${r.error}`;
};

on("snapshot", async m => {
  Object.values(m.frames || {}).forEach(onFrame);
  if (m.history.length) renderTelemetry(m.history[m.history.length - 1]);
  m.writer_log.slice().reverse().forEach(logLine);
  $("#req-topic").textContent = m.request_topic;
  if (!$("#raw").value) $("#raw").value = JSON.stringify(await api("api/request-template"), null, 2);
  if (m.writer_status) $("#writer-status").textContent = JSON.stringify(m.writer_status);
});
on("frame", onFrame);
on("telemetry", renderTelemetry);
on("writer_log", logLine);
on("writer_status", m => ($("#writer-status").textContent = JSON.stringify(m.payload)));

setupSettings();
connectSocket();
