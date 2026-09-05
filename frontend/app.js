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
// standing/pooled water, not a hazard-red alert; borderless (weight 0)
// for a cleaner look. (An overlap-based darkening effect was considered
// but dropped: checked against real output -- 5,084 features in one run
// -- and bluespot polygons never actually overlap each other, so there
// was nothing to darken.)
//
// interactive:false on every one of these: none of them have a click
// handler or popup, so leaving them clickable only caused harm -- once
// any of these render on top of the Kloakoplande layer, they'd silently
// swallow clicks meant for whatever plan-area polygon is underneath.
const RESULT_LAYER_STYLES = {
  "Opland": { color: "#082f3a", weight: 3, fillColor: "#0a4a5c", fillOpacity: 0.22, kind: "polygon", interactive: false },
  "Selected ID15": { color: "#6b7280", weight: 1.5, dashArray: "5,4", fillOpacity: 0, kind: "polygon", interactive: false },
  "Bluespot": { color: "#1e40af", weight: 0, fillColor: "#1e40af", fillOpacity: 0.55, kind: "polygon", interactive: false },
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

// Draw order, bottom to top -- split around where the analyzed-area
// overlay sits (see ANALYZED_AREA_STYLE below): Opland/Bluespot/Selected
// ID15 stay underneath it, the two stream layers stay on top of it, so
// streams read as continuous across the AOI boundary and the AOI itself
// still stays visible over Bluespot.
const RESULT_LAYER_ORDER_BELOW_AOI = ["Selected ID15", "Opland", "Bluespot"];
const RESULT_LAYER_ORDER_ABOVE_AOI = ["Vandveje (ID15)", "Vandveje (Opland)"];

// The polygon actually submitted for analysis (a selected Kloakoplande
// feature or a freehand drawing) is styled as a cartographic AOI marker,
// not another data layer -- solid black border, diagonal-hatch fill (see
// initAoiHatchPattern) rather than a solid color. That's a deliberate
// choice, not just matching the reference figure: black holds contrast
// against both basemaps (colorful street map and photographic aerial
// alike), whereas any other saturated color risks blending into one of
// them; hachures are the standard cartographic convention for "extent of
// analysis" precisely because they read as a structural frame rather
// than a result. The hatch's open gaps keep the bluespot/opland fills
// underneath visible; it sits below the two stream layers so their
// channels stay unbroken crossing it, and above everything else so it
// doesn't get swallowed by Bluespot's solid fill.
const ANALYZED_AREA_STYLE = { color: "#000000", weight: 3, fillColor: "url(#aoi-hatch)", fillOpacity: 1, interactive: false };

let map, kloakLayer, drawnLayer, drawControl, aoiRenderer;
let selectedGeometry = null;
let selectedLabel = null;
let selectedAreaCode = null;
let currentMode = "select";
let resultLayers = {};
let analyzedAreaLayer = null;
let isRunning = false;

// Basemap toggle (street map <-> Dataforsyningen aerial orthophoto), Google
// Maps style: a small live preview square, bottom-left, showing whichever
// basemap you'd switch TO. Each basemap needs its own tile-layer instance
// per map (a Leaflet tile layer belongs to one map at a time), hence the
// separate *Main/*Mini pairs below.
let osmMain, ortofotoMain, osmMini, ortofotoMini, miniMap;
let activeBasemap = "osm";

function makeOsmLayer() {
  return L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
  });
}

// Routed through our own backend, not Dataforsyningen directly -- the WMS
// call needs an API key, and that key is a credential that must never end
// up in the public frontend JS or the public repo (see backend/app.py).
function makeOrtofotoLayer() {
  return L.tileLayer.wms(`${API_BASE}/tiles/ortofoto`, {
    layers: "orto_foraar",
    format: "image/png",
    version: "1.1.1",
    transparent: false,
    maxZoom: 19,
    attribution: "Ortofoto: Dataforsyningen/SDFI",
  });
}

// Combined multiplier applied to both stream layers' min/max radius, live-
// adjustable via the sidebar slider so you can tune the look without
// re-running the analysis (it's a pure restyle -- the underlying data and
// each point's rank don't change). Persists across runs rather than
// resetting to 1 each time, so a chosen setting stays put.
let streamWidthScale = 1;

function initMap() {
  map = L.map("map", { preferCanvas: true, zoomControl: false });
  L.control.zoom({ position: "bottomright" }).addTo(map);
  osmMain = makeOsmLayer().addTo(map);
  ortofotoMain = makeOrtofotoLayer();
  map.setView([55.913, 9.322], 14);
}

// The map renders vectors to a canvas (preferCanvas above) for
// performance, but canvas can't do pattern fills -- only the AOI overlay
// needs one, so it gets its own dedicated SVG renderer, with a diagonal-
// hatch <pattern> injected into that renderer's own <svg> once up front.
// ANALYZED_AREA_STYLE references it by id ("url(#aoi-hatch)"), the same
// way any ordinary SVG fill color would be referenced.
function initAoiHatchPattern() {
  aoiRenderer = L.svg().addTo(map);
  const svg = aoiRenderer._container;
  const ns = "http://www.w3.org/2000/svg";

  const pattern = document.createElementNS(ns, "pattern");
  pattern.setAttribute("id", "aoi-hatch");
  pattern.setAttribute("width", "8");
  pattern.setAttribute("height", "8");
  pattern.setAttribute("patternUnits", "userSpaceOnUse");
  pattern.setAttribute("patternTransform", "rotate(45)");

  const line = document.createElementNS(ns, "line");
  line.setAttribute("x1", "0");
  line.setAttribute("y1", "0");
  line.setAttribute("x2", "0");
  line.setAttribute("y2", "8");
  line.setAttribute("stroke", "#000000");
  line.setAttribute("stroke-width", "1.5");
  pattern.appendChild(line);

  const defs = document.createElementNS(ns, "defs");
  defs.appendChild(pattern);
  svg.insertBefore(defs, svg.firstChild);
}

function initBasemapToggle() {
  const control = L.control({ position: "bottomleft" });
  control.onAdd = function () {
    const container = L.DomUtil.create("div", "basemap-toggle");
    container.innerHTML =
      '<div class="basemap-toggle-thumb" id="basemap-toggle-thumb"></div>' +
      '<span class="basemap-toggle-label" id="basemap-toggle-label">Ortofoto</span>';
    L.DomEvent.disableClickPropagation(container);
    L.DomEvent.on(container, "click", toggleBasemap);
    return container;
  };
  control.addTo(map);

  miniMap = L.map("basemap-toggle-thumb", {
    zoomControl: false,
    attributionControl: false,
    dragging: false,
    scrollWheelZoom: false,
    doubleClickZoom: false,
    boxZoom: false,
    keyboard: false,
    touchZoom: false,
    tap: false,
    fadeAnimation: false,
  });
  // Mini map always previews the basemap you'd switch TO, i.e. whichever
  // one is NOT active on the main map right now.
  ortofotoMini = makeOrtofotoLayer().addTo(miniMap);
  miniMap.setView(map.getCenter(), map.getZoom());

  map.on("move zoom", () => miniMap.setView(map.getCenter(), map.getZoom(), { animate: false }));
}

function toggleBasemap() {
  const label = document.getElementById("basemap-toggle-label");
  if (activeBasemap === "osm") {
    map.removeLayer(osmMain);
    ortofotoMain.addTo(map);
    miniMap.removeLayer(ortofotoMini);
    osmMini = osmMini || makeOsmLayer();
    osmMini.addTo(miniMap);
    label.textContent = "Kort";
    activeBasemap = "ortofoto";
  } else {
    map.removeLayer(ortofotoMain);
    osmMain.addTo(map);
    miniMap.removeLayer(osmMini);
    ortofotoMini = ortofotoMini || makeOrtofotoLayer();
    ortofotoMini.addTo(miniMap);
    label.textContent = "Ortofoto";
    activeBasemap = "osm";
  }
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
    selectedLabel = "Selvtegnet område";
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
      // Clicking the already-selected feature again deselects it, rather
      // than being a one-way trip that only another selection can undo.
      layer.on("click", () => {
        if (isRunning) return;
        if (feature.properties.navn1201 === selectedAreaCode) clearSelection();
        else selectAreaFeature(feature, layer);
      });
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
      if (isRunning) return;
      if (code === selectedAreaCode) { clearSelection(); return; }
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
  selectedLabel = `Planområde ${feature.properties.navn1201}`;
  selectedAreaCode = feature.properties.navn1201;
  updateSelectionUI();
}

// Also resets any Kloakoplande highlight -- covers both an explicit
// deselect click and switching away from "select" mode, which previously
// left the last-selected plan area visually highlighted even after its
// selection no longer meant anything.
function clearSelection() {
  if (kloakLayer) kloakLayer.eachLayer((l) => kloakLayer.resetStyle(l));
  document.querySelectorAll(".area-item").forEach((el) => el.classList.remove("is-selected"));
  selectedAreaCode = null;
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
  if (analyzedAreaLayer) {
    map.removeLayer(analyzedAreaLayer);
    analyzedAreaLayer = null;
  }
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

// Re-pins the analyzed-area outline and the two stream layers back to
// the front, in that order, after any legend checkbox re-adds a layer
// (re-adding always puts it on top again, which would otherwise bury
// whichever of these is supposed to stay above it).
function reassertAoiAndStreamOrder() {
  if (analyzedAreaLayer) analyzedAreaLayer.bringToFront();
  RESULT_LAYER_ORDER_ABOVE_AOI.forEach((name) => {
    if (resultLayers[name]) resultLayers[name].bringToFront();
  });
}

function renderResults(layers, label, geometry) {
  clearResultLayers();
  const legend = document.getElementById("legend");
  document.getElementById("results-for").textContent = label;

  function addResultLayer(name) {
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
            interactive: false,
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
      if (e.target.checked) {
        layer.addTo(map);
        reassertAoiAndStreamOrder();
      } else {
        map.removeLayer(layer);
      }
    });
    legend.appendChild(li);
  }

  // Add order matters here (see RESULT_LAYER_ORDER_* comment above): the
  // AOI overlay is created in between the two groups, so it naturally
  // lands above Opland/Bluespot/Selected ID15 and below the streams
  // without needing bringToFront() on the very first render.
  RESULT_LAYER_ORDER_BELOW_AOI.forEach(addResultLayer);

  if (geometry) {
    analyzedAreaLayer = L.geoJSON(
      { type: "Feature", geometry, properties: {} },
      { style: () => ANALYZED_AREA_STYLE, renderer: aoiRenderer }
    ).addTo(map);
  }

  RESULT_LAYER_ORDER_ABOVE_AOI.forEach(addResultLayer);

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
      throw new Error(data.error || "Analysen fejlede.");
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
  showStatus("Kører analyse…");
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
    if (!res.ok) throw new Error(`Forespørgsel mislykkedes (${res.status})`);
    const { job_id } = await res.json();
    const layers = await pollJob(job_id);
    renderResults(layers, analyzedLabel, analyzedGeometry);
  } catch (err) {
    showToast(err.message || "Der gik noget galt.");
  } finally {
    hideStatus();
    setUILocked(false);
  }
}

function init() {
  initMap();
  initAoiHatchPattern();
  initBasemapToggle();
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
