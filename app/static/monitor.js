const WINDOW_S = 300;
const COLORS = ["#ff7a1a", "#4dabf7", "#2ecc71", "#f5b041", "#c77dff", "#ff4d4f"];
Chart.defaults.color = "#8b98a8";
Chart.defaults.borderColor = "#243040";
Chart.defaults.font.size = 11;

const showIr = cameraView("#ir", "#ir-label", "#ir-empty", "IR");
on("frame", f => f.camera_kind === "ir" && showIr(f));

function lineChart(id, series) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { datasets: series.map((label, i) => ({ label, data: [], borderColor: COLORS[i], borderWidth: 1.5,
      pointRadius: 0, spanGaps: false })) },
    options: {
      animation: false, maintainAspectRatio: false, parsing: false,
      scales: { x: { type: "linear", title: { display: true, text: "seconds ago" }, reverse: true, min: 0, max: WINDOW_S } },
      plugins: { legend: { display: series.length > 1, labels: { boxWidth: 10 } } },
    },
  });
}

const charts = {
  temp: [lineChart("c-temp", ["melt", "previous layer"]), m => [m.melt, m.substrate]],
  flow: [lineChart("c-flow", ["flow %"]), m => [m.flow_pct]],
  motion: [lineChart("c-motion", ["velocity mm/s", "extrusion speed"]), m => [m.vel, m.extrusion_speed]],
  cool: [lineChart("c-cool", ["cooling output", "fan 1"]), m => [m.cooling, m.fan0]],
  z: [lineChart("c-z", ["z"]), m => [m.pos ? m.pos.z : null]],
};
const rateChart = lineChart("c-rate", []);
const samples = [];  // telemetry metrics with .t (ms)
const rateSamples = [];  // {t, rates: {topic: rate}}

function pushSample(m) {
  if (samples.length && m.t - samples[samples.length - 1].t < 500) return;
  samples.push(m);
  while (samples.length && Date.now() - samples[0].t > WINDOW_S * 1000) samples.shift();
}

function redraw() {
  const now = Date.now();
  for (const [chart, pick] of Object.values(charts)) {
    chart.data.datasets.forEach((ds, i) => {
      ds.data = samples.map(m => ({ x: (now - m.t) / 1000, y: pick(m)[i] ?? null }));
    });
    chart.update("none");
  }
  const topics = [...new Set(rateSamples.flatMap(s => Object.keys(s.rates)))];
  rateChart.data.datasets = topics.map((topic, i) => ({
    label: topic.replace("demex/demo/mini/", ""), borderColor: COLORS[i % COLORS.length], borderWidth: 1.5,
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
  const rows = Object.entries(topics).map(([topic, st]) => {
    const stale = st.age_ms > 5000 ? "stale" : "";
    return `<tr class="topic ${stale}" data-topic="${topic}"><td>${topic}</td><td>${st.count}</td><td>${st.rate}</td>
      <td>${(st.age_ms / 1000).toFixed(1)} s ago</td><td>${st.bytes ?? "—"} B</td></tr>`;
  });
  $("#topics").innerHTML = rows.join("");
  $("#topic-count").textContent = `${Object.keys(topics).length} topics`;
  document.querySelectorAll("tr.topic").forEach(tr => (tr.onclick = () => { selectedTopic = tr.dataset.topic; showPayload(); }));
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
});
on("telemetry", pushSample);
on("topics", m => renderTopics(m.topics));

connectSocket();
