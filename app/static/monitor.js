const WINDOW_S = 300;
const COLORS = ["oklch(0.74 0.165 52)", "oklch(0.76 0.12 235)", "oklch(0.78 0.16 148)", "oklch(0.84 0.15 85)",
  "oklch(0.72 0.15 305)", "oklch(0.69 0.19 27)"];
const css = getComputedStyle(document.documentElement);
Chart.defaults.color = css.getPropertyValue("--muted").trim();
Chart.defaults.borderColor = css.getPropertyValue("--line-soft").trim();
Chart.defaults.font.family = css.getPropertyValue("--mono").trim();
Chart.defaults.font.size = 11;

const showIr = cameraView("#ir", "#ir-label", "#ir-empty", "IR");
on("frame", f => f.camera_kind === "ir" && showIr(f));

function lineChart(id, series, { gap = 1.5, steps = [] } = {}) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { datasets: series.map((label, i) => ({ label, data: [], borderColor: COLORS[i], borderWidth: 1.5,
      pointRadius: 0, spanGaps: gap, stepped: steps.includes(i) ? "before" : false,
      borderDash: steps.includes(i) ? [5, 4] : [] })) },  // break the line where samples are >gap s apart (outages)
    options: {
      animation: false, maintainAspectRatio: false, parsing: false,
      scales: { x: { type: "linear", title: { display: true, text: "seconds ago" }, reverse: true, min: 0, max: WINDOW_S } },
      plugins: { legend: { display: series.length > 1, labels: { boxWidth: 10 } } },
    },
  });
}

const charts = {
  temp: [lineChart("c-temp", ["nozzle", "melt (IR)", "previous layer (IR)", "bed"]),
    m => [m.nozzle?.current, m.melt, m.substrate, m.bed?.current]],
  flow: [lineChart("c-flow", ["extrusion multiplier %"]), m => [m.flow_pct]],
  motion: [lineChart("c-motion", ["velocity mm/s"]), m => [m.vel]],
  espeed: [lineChart("c-espeed", ["extrusion speed"]), m => [m.extrusion_speed]],
  cool: [lineChart("c-cool", ["cooling output", "fan 1"]), m => [m.cooling, m.fan0]],
  z: [lineChart("c-z", ["z"]), m => [m.pos ? m.pos.z : null]],
};
const widthChart = lineChart("c-width", ["width mm", "target mm"], { gap: 3, steps: [1] });
// The camera is blind above its ceiling: a dashed line for it, and hollow markers for samples that only mean "at least".
widthChart.data.datasets.push(
  { label: "camera ceiling", data: [], borderColor: css.getPropertyValue("--muted").trim(), borderWidth: 1, borderDash: [2, 4],
    pointRadius: 0, spanGaps: 3 },
  { label: "at ceiling (at least)", data: [], showLine: false, borderColor: COLORS[0], backgroundColor: "transparent", borderWidth: 1.5,
    pointStyle: "circle", pointRadius: 3.5, pointBorderWidth: 1.5, pointBackgroundColor: "transparent", pointBorderColor: COLORS[0] });
const widthSamples = [];  // {t, mm, atCeiling} real-width estimates, t = when measured (ms)
const targetSamples = [];  // {t, target} operator target as polled; target null = none
const ceilingSamples = [];  // {t, ceiling} camera ceiling as polled; null = none
// Without a ceiling in the camera model these two series stay empty, so their legend entries are hidden.
widthChart.options.plugins.legend.labels.filter = item => item.datasetIndex < 2 || widthChart.data.datasets[item.datasetIndex].data.some(p => p.y !== null);
const rateChart = lineChart("c-rate", []);
const samples = [];  // telemetry metrics with .t (ms)
const rateSamples = [];  // {t, rates: {topic: rate}}

function pushSample(m) {
  if (samples.length && m.t - samples[samples.length - 1].t < 500) return;
  samples.push(m);
  while (samples.length && Date.now() - samples[0].t > WINDOW_S * 1000) samples.shift();
}

const trim = arr => { while (arr.length && Date.now() - arr[0].t > WINDOW_S * 1000) arr.shift(); };
const isNum = v => typeof v === "number" && Number.isFinite(v);

function redraw() {
  const now = Date.now();
  trim(widthSamples);
  trim(targetSamples);
  trim(ceilingSamples);
  widthChart.data.datasets[0].data = widthSamples.map(s => ({ x: (now - s.t) / 1000, y: s.mm }));
  widthChart.data.datasets[1].data = targetSamples.map(s => ({ x: (now - s.t) / 1000, y: s.target }));
  widthChart.data.datasets[2].data = ceilingSamples.map(s => ({ x: (now - s.t) / 1000, y: s.ceiling }));
  const bucketed = new Map();  // one marker per 4 s so a long stay at the ceiling stays readable
  for (const s of widthSamples) if (s.atCeiling && !bucketed.has(Math.floor(s.t / 4000))) bucketed.set(Math.floor(s.t / 4000), s);
  widthChart.data.datasets[3].data = [...bucketed.values()].map(s => ({ x: (now - s.t) / 1000, y: s.mm }));
  widthChart.update("none");
  for (const [chart, pick] of Object.values(charts)) {
    chart.data.datasets.forEach((ds, i) => {
      ds.data = samples.map(m => ({ x: (now - m.t) / 1000, y: pick(m)[i] ?? null }));
    });
    chart.update("none");
  }
  const topics = [...new Set(rateSamples.flatMap(s => Object.keys(s.rates)))];
  rateChart.data.datasets = topics.map((topic, i) => ({
    label: topic.replace("demex/demo/mini/", "").replace("demex/hackathon/mini/extrusion/", "writer/"), borderColor: COLORS[i % COLORS.length], borderWidth: 1.5,
    pointRadius: 0, data: rateSamples.map(s => ({ x: (now - s.t) / 1000, y: s.rates[topic] ?? 0 })),
  }));
  rateChart.options.plugins.legend.display = true;
  rateChart.update("none");
}
setInterval(redraw, 1000);

let selectedTopic = null;
let lastTopics = {};
function renderTopics(topics) {
  lastTopics = topics;
  $("#topics").replaceChildren(...Object.entries(topics).map(([topic, st]) => {
    const tr = document.createElement("tr");
    tr.className = st.age_ms > 5000 ? "topic stale" : "topic";
    tr.dataset.topic = topic;
    tr.onclick = () => { selectedTopic = topic; showPayload(); };
    for (const text of [topic, st.count, st.rate, `${(st.age_ms / 1000).toFixed(1)} s ago`, `${st.bytes ?? "—"} B`]) {
      tr.append(Object.assign(document.createElement("td"), { textContent: text }));
    }
    return tr;
  }));
  $("#topic-count").textContent = `${Object.keys(topics).length} topics`;
  showPayload();
  rateSamples.push({ t: Date.now(), rates: Object.fromEntries(Object.entries(topics).map(([k, v]) => [k, v.rate])) });
  while (rateSamples.length && Date.now() - rateSamples[0].t > WINDOW_S * 1000) rateSamples.shift();
}

function showPayload() {
  if (!selectedTopic || !lastTopics[selectedTopic]) return;
  $("#payload-topic").textContent = selectedTopic;
  $("#payload").textContent = JSON.stringify(lastTopics[selectedTopic].last, null, 2);
}

on("snapshot", m => {
  m.history.forEach(pushSample);
  if (m.frames && m.frames.ir) showIr(m.frames.ir);
  redraw();
});
on("telemetry", pushSample);
on("topics", m => renderTopics(m.topics));
on("width", m => {
  if (isNum(m.mm)) widthSamples.push({ t: Date.now() - (isNum(m.age_ms) ? m.age_ms : 0), mm: m.mm, atCeiling: m.at_ceiling === true });
});
on("control", c => {
  if (c.offline) return;
  targetSamples.push({ t: Date.now(), target: isNum(c.target_mm) ? c.target_mm : null });
  ceilingSamples.push({ t: Date.now(), ceiling: isNum(c.ceiling_mm) ? c.ceiling_mm : null });
});
startControlPoll();

connectSocket();
