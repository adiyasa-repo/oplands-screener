// API base: talk to a local backend during development, the deployed one
// once this is actually hosted.
const API_BASE = (location.hostname === "localhost" || location.hostname === "127.0.0.1")
  ? "http://127.0.0.1:8000"
  : "https://api.adiyasa.dk";

// Layer styling -- names match the QGIS project's own layer names exactly.
// Opland: dark slate-teal border and fill, well past the original
// #0e7490 -- needs to read as a clearly-bounded catchment outline even
// against the other teal-ish map elements, not just a faint tint.
// Bluespot: switched from red to a deep navy blue -- it indicates
// standing/pooled water, not a hazard-red alert.
const RESULT_LAYER_STYLES = {
  "Opland": { color: "#082f3a", weight: 3, fillColor: "#0a4a5c", fillOpacity: 0.22, kind: "polygon" },
  "Selected ID15": { color: "#6b7280", weight: 1.5, dashArray: "5,4", fillOpacity: 0, kind: "polygon" },
  "Bluespot": { color: "#1e3a8a", weight: 1, fillColor: "#1e40af", fillOpacity: 0.55, kind: "polygon" },
  // Point layers: circle radius scales with each point's own sampled flow-
  // accumulation value (the "resampled_1" field QGIS's own graduated-size
  // styles use -- see ID15Streams.qml/CloudburstStreams.qml,
  // graduatedMethod="GraduatedSize" scale_method="diameter" on that exact
  // field), not a fixed dot size. Small streams stay small; the channels
  // carrying real volume visibly grow. Min/max radius are picked per
  // result from that layer's own value range (see sizeForValue below),
  // same idea as QGIS classifying against the loaded data, not a fixed
  // universal scale.
  "Vandveje (Opland)": { color: "#2563eb", kind: "point", valueField: "resampled_1", minRadius: 1.5, maxRadius: 16 },
  "Vandveje (ID15)": { color: "#60a5fa", kind: "point", valueField: "resampled_1", minRadius: 1, maxRadius: 11 },
};

// Draw order, bottom to top.
const RESULT_LAYER_ORDER = ["Selected ID15", "Opland", "Vandveje (ID15)", "Vandveje (Opland)", "Bluespot"];

let map, kloakLayer, drawnLayer, drawControl;
let selectedGeometry = null;
let selectedLabel = null;
let currentMode = "select";
let resultLayers = {};
let isRunning = false;

// Combined multiplier applied to both stream layers' min/max radius, live-
// adjustable via the sidebar slider so you can tune the look without
// re-running the analysis (it's a pure restyle -- the underlying data and
// each point's rank don't change). Persists across runs rather than
// resetting to 1 each time, so a chosen setting stays put.
let streamWidthScale = 1;

function initMap() {
  map = L.map("map", { preferCanvas: true, zoomControl: false });
  L.control.zoom({ position: "bottomright" }).addTo(map);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(map);
  map.setView([55.913, 9.322], 14);
}

function initDrawing() {
  const drawnItems = new L.FeatureGroup();
  map.addLayer(drawnItems);
  drawControl = new L.Draw.Polygon(map, {
    shapeOptions: { color: "#0e7490", weight: 2, fillOpacity: 0.1 },
  });

  map.on(L.Draw.Event.CREATED, (e) => {
    if (isRunning) return;
    drawnItems.clearLayers();
    drawnItems.addLayer(e.layer);
    drawnLayer = e.layer;
    selectedGeometry = e.layer.toGeoJSON().geometry;
    selectedLabel = "Custom drawn area";
    document.getElementById("clear-draw").classList.remove("is-hidden");
    updateSelectionUI();
  });

  document.getElementById("start-draw").addEventListener("click", () => {
    drawControl.enable();
  });
  document.getElementById("clear-draw").addEventListener("click", () => {
    drawnItems.clearLayers();
    drawnLayer = null;
    if (currentMode === "draw") clearSelection();
  });
}

async function loadKloakoplande() {
  const res = await fetch("data/kloakoplande.geojson");
  const geojson = await res.json();

  // Bright orange -- deliberately far from the teal used everywhere else
  // on the map, so the selectable plan-area layer reads as its own thing
  // at a glance, not just another shade of the base styling.
  kloakLayer = L.geoJSON(geojson, {
    style: () => ({ color: "#c2410c", weight: 1.5, fillColor: "#f97316", fillOpacity: 0.22 }),
    onEachFeature: (feature, layer) => {
      layer.on("click", () => selectAreaFeature(feature, layer));
    },
  }).addTo(map);

  map.fitBounds(kloakLayer.getBounds(), { padding: [40, 40] });

  const areas = geojson.features
    .map((f) => f.properties.navn1201)
    .sort();
  renderAreaList(areas, geojson);
}

function renderAreaList(codes, geojson) {
  const list = document.getElementById("area-list");
  list.innerHTML = "";
  codes.forEach((code) => {
    const feature = geojson.features.find((f) => f.properties.navn1201 === code);
    const li = document.createElement("li");
    li.className = "area-item";
    li.dataset.code = code;
    li.innerHTML = `<span class="code">${code}</span><span class="status-dot" title="${feature.properties.status || ""}"></span>`;
    li.addEventListener("click", () => {
      const layer = findKloakLayerByCode(code);
      if (layer) selectAreaFeature(feature, layer);
    });
    list.appendChild(li);
  });
}

function findKloakLayerByCode(code) {
  let found = null;
  kloakLayer.eachLayer((layer) => {
    if (layer.feature.properties.navn1201 === code) found = layer;
  });
  return found;
}

function selectAreaFeature(feature, layer) {
  if (isRunning) return;
  kloakLayer.eachLayer((l) => kloakLayer.resetStyle(l));
  layer.setStyle({ color: "#9a3412", weight: 3, fillColor: "#f97316", fillOpacity: 0.45 });
  layer.bringToFront();

  document.querySelectorAll(".area-item").forEach((el) => {
    el.classList.toggle("is-selected", el.dataset.code === feature.properties.navn1201);
  });

  selectedGeometry = feature.geometry;
  selectedLabel = `Plan area ${feature.properties.navn1201}`;
  updateSelectionUI();
}

function clearSelection() {
  selectedGeometry = null;
  selectedLabel = null;
  updateSelectionUI();
}

function updateSelectionUI() {
  const panel = document.getElementById("selection-panel");
  const runBtn = document.getElementById("run-btn");
  if (selectedGeometry) {
    panel.classList.remove("is-hidden");
    document.getElementById("selection-value").textContent = selectedLabel;
    runBtn.disabled = isRunning;
  } else {
    panel.classList.add("is-hidden");
    runBtn.disabled = true;
  }
}

// Prevents changing the selection (and misleadingly relabeling it) while a
// job for a previously-selected area is still running -- caught during
// testing: clicking a different area mid-run left the sidebar showing one
// area while the in-flight (and soon-rendered) results were for another.
function setUILocked(locked) {
  isRunning = locked;
  document.querySelectorAll(".mode-tab, #start-draw, #clear-draw").forEach((el) => {
    el.disabled = locked;
  });
  document.getElementById("area-list").classList.toggle("is-locked", locked);
  document.getElementById("run-btn").disabled = locked || !selectedGeometry;
}

function setMode(mode) {
  currentMode = mode;
  document.querySelectorAll(".mode-tab").forEach((btn) => {
    const active = btn.dataset.mode === mode;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active);
  });
  document.querySelectorAll(".mode-panel").forEach((panel) => {
    panel.classList.toggle("is-hidden", panel.dataset.modePanel !== mode);
  });
  clearSelection();
  if (mode === "select") {
    drawControl && drawControl.disable();
  }
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.remove("is-hidden");
  setTimeout(() => toast.classList.add("is-hidden"), 5000);
}

function showStatus(text) {
  document.getElementById("status-overlay").classList.remove("is-hidden");
  document.getElementById("status-text").textContent = text;
}
function hideStatus() {
  document.getElementById("status-overlay").classList.add("is-hidden");
}

function clearResultLayers() {
  Object.values(resultLayers).forEach((layer) => map.removeLayer(layer));
  resultLayers = {};
  document.getElementById("legend").innerHTML = "";
  document.getElementById("results-panel").classList.add("is-hidden");
}

// Percentile-rank size scaling, not raw-value linear scaling. Confirmed
// against real output (see project notes): flow accumulation is heavily
// right-skewed -- for a real run, 79-90% of points fell within 10% of
// that layer's own max value. A plain linear value->radius map crams
// that entire majority near minRadius and only lets a couple of outlier
// trunk-channel points grow, which reads as visually flat -- exactly the
// problem reported. QGIS's own styling (ID15Streams.qml /
// CloudburstStreams.qml) sidesteps this with 50 *quantile* classes
// (equal population per bin, not equal value-width), which is really a
// discretized rank-based size mapping. This reproduces that continuously:
// a point's radius depends on where its value ranks among this result's
// own points, not on the raw magnitude, so the size differences spread
// across the whole population instead of collapsing onto a few outliers.
function buildRankLookup(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return (value) => {
    if (!sorted.length || !isFinite(value) || value <= 0) return 0;
    // Index of the first element > value == count of elements <= value.
    let lo = 0, hi = sorted.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (sorted[mid] <= value) lo = mid + 1; else hi = mid;
    }
    return lo / sorted.length;
  };
}

function radiusForRank(rank, style, scale) {
  return (style.minRadius + rank * (style.maxRadius - style.minRadius)) * scale;
}

// Re-applies the current streamWidthScale to whatever stream layers are
// already on the map, using each marker's stored rank -- no re-fetch, no
// re-run, just a restyle. Safe to call even when no results are rendered
// yet (both lookups just come back empty).
function applyStreamWidthScale() {
  ["Vandveje (ID15)", "Vandveje (Opland)"].forEach((name) => {
    const layer = resultLayers[name];
    const style = RESULT_LAYER_STYLES[name];
    if (!layer) return;
    layer.eachLayer((marker) => {
      marker.setRadius(radiusForRank(marker.options.radiusRank, style, streamWidthScale));
    });
  });
}

function renderResults(layers, label) {
  clearResultLayers();
  const legend = document.getElementById("legend");
  document.getElementById("results-for").textContent = label;

  RESULT_LAYER_ORDER.forEach((name) => {
    const geojson = layers[name];
    const style = RESULT_LAYER_STYLES[name];
    if (!geojson || !style) return;

    const count = geojson.features.length;
    let layer;
    if (style.kind === "point") {
      const values = geojson.features
        .map((f) => Number(f.properties?.[style.valueField]))
        .filter((v) => isFinite(v) && v > 0);
      const rankOf = buildRankLookup(values);
      layer = L.geoJSON(geojson, {
        pointToLayer: (feature, latlng) => {
          const rank = rankOf(Number(feature.properties?.[style.valueField]));
          return L.circleMarker(latlng, {
            // radiusRank is stored (not just the computed radius) so the
            // width slider can recompute it live against a new scale
            // without redoing the rank lookup or touching the data at all.
            radius: radiusForRank(rank, style, streamWidthScale),
            radiusRank: rank,
            color: style.color, weight: 0, fillColor: style.color, fillOpacity: 0.85,
          });
        },
      });
    } else {
      layer = L.geoJSON(geojson, { style: () => style });
    }
    layer.addTo(map);
    resultLayers[name] = layer;

    const li = document.createElement("li");
    li.className = "legend-item";
    li.innerHTML = `
      <input type="checkbox" checked>
      <span class="legend-swatch" style="background:${style.fillColor || style.color}"></span>
      <span class="legend-name">${name}</span>
      <span class="legend-count">${count.toLocaleString()}</span>
    `;
    li.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) layer.addTo(map);
      else map.removeLayer(layer);
    });
    legend.appendChild(li);
  });

  // Reflect the persisted scale in the slider itself, in case it was
  // adjusted on a previous run -- it shouldn't silently reset to 1 here.
  document.getElementById("stream-width-slider").value = streamWidthScale;
  document.getElementById("stream-width-value").textContent = `${streamWidthScale.toFixed(1)}×`;

  document.getElementById("results-panel").classList.remove("is-hidden");

  const oplandLayer = resultLayers["Opland"];
  if (oplandLayer) map.fitBounds(oplandLayer.getBounds(), { padding: [60, 60] });
}

async function pollJob(jobId) {
  const started = Date.now();
  const elapsedEl = document.getElementById("status-elapsed");
  const tick = setInterval(() => {
    elapsedEl.textContent = `${Math.floor((Date.now() - started) / 1000)}s`;
  }, 1000);

  while (true) {
    await new Promise((r) => setTimeout(r, 2000));
    const res = await fetch(`${API_BASE}/jobs/${jobId}`);
    const data = await res.json();
    if (data.status === "done") {
      clearInterval(tick);
      return data.layers;
    }
    if (data.status === "error") {
      clearInterval(tick);
      throw new Error(data.error || "Analysis failed.");
    }
  }
}

async function runAnalysis() {
  if (!selectedGeometry || isRunning) return;

  // Captured now, deliberately: the selection must not be allowed to
  // change while this specific job is running (see setUILocked), so
  // whatever the current selection is at this instant is what the
  // eventual results actually belong to.
  const analyzedGeometry = selectedGeometry;
  const analyzedLabel = selectedLabel;

  setUILocked(true);
  showStatus("Running analysis…");
  try {
    const res = await fetch(`${API_BASE}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(analyzedGeometry),
    });
    if (res.status === 429) {
      const data = await res.json();
      throw new Error(data.detail);
    }
    if (!res.ok) throw new Error(`Request failed (${res.status})`);
    const { job_id } = await res.json();
    const layers = await pollJob(job_id);
    renderResults(layers, analyzedLabel);
  } catch (err) {
    showToast(err.message || "Something went wrong.");
  } finally {
    hideStatus();
    setUILocked(false);
  }
}

function init() {
  initMap();
  initDrawing();
  loadKloakoplande();

  document.querySelectorAll(".mode-tab").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });

  document.getElementById("area-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll(".area-item").forEach((el) => {
      el.style.display = el.dataset.code.toLowerCase().includes(q) ? "" : "none";
    });
  });

  document.getElementById("run-btn").addEventListener("click", runAnalysis);

  document.getElementById("stream-width-slider").addEventListener("input", (e) => {
    streamWidthScale = Number(e.target.value);
    document.getElementById("stream-width-value").textContent = `${streamWidthScale.toFixed(1)}×`;
    applyStreamWidthScale();
  });
}

init();
