// API base: talk to a local backend during development. In production the
// frontend and API share one origin (opland.adiyasa.dk) -- Caddy proxies
// /api/* to the backend -- so a relative path is all that's needed there,
// and it means the browser never makes a cross-origin request at all.
const API_BASE = (location.hostname === "localhost" || location.hostname === "127.0.0.1")
  ? "http://127.0.0.1:8000"
  : "/api";

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

// The complete map draw order, bottom to top, in one list -- everything
// this app puts on the map, AOI included, reads its stacking from this
// single source of truth (see reassertMapLayerOrder and renderResults).
//
// One real constraint on that "single list" framing, worth being honest
// about rather than glossing over: "AOI" isn't drawn by the same
// mechanism as everything after it. Canvas can't do the hatch's pattern
// fill, so the AOI overlay lives on its own SVG renderer, in its own
// Leaflet pane, pinned by a fixed CSS z-index below the shared canvas
// pane every other entry here draws to (see initAoiHatchPattern). A
// pane's z-index is an absolute number, not a per-item position -- it
// can only put the AOI entirely below or entirely above ALL canvas
// content, never threaded between two canvas items the way this list's
// syntax might suggest. That's exactly the bug this list replaced: it
// used to be assumed that insertion order plus bringToFront() calls were
// enough to keep the AOI sandwiched between Opland and the streams --
// confirmed by testing that this was never actually true, since the
// AOI's pane sat above the canvas regardless of any per-layer call. So
// "AOI" stays first (bottom) here on purpose, matching where it actually
// ends up -- move it anywhere but the very bottom of this list and the
// code would be lying about what the browser actually renders.
//
// Below AOI's line -- among the layers that DO share one real canvas --
// this list means exactly what it looks like: Bluespot last, so it's the
// single topmost thing this app ever draws.
const MAP_LAYER_ORDER = ["AOI", "Selected ID15", "Opland", "Vandveje (ID15)", "Vandveje (Opland)", "Bluespot"];

// The polygon actually submitted for analysis (a selected reference-layer
// feature or a freehand drawing) is styled as a cartographic AOI marker,
// not another data layer -- solid black border, diagonal-hatch fill (see
// initAoiHatchPattern) rather than a solid color. That's a deliberate
// choice, not just matching the reference figure: black holds contrast
// against both basemaps (colorful street map and photographic aerial
// alike), whereas any other saturated color risks blending into one of
// them; hachures are the standard cartographic convention for "extent of
// analysis" precisely because they read as a structural frame rather
// than a result. It now sits in its own pane below every canvas-drawn
// layer, Opland included -- see the comment on RESULT_LAYER_ORDER_* above
// for why, and why that's an intentional tradeoff.
const ANALYZED_AREA_STYLE = { color: "#000000", weight: 3, fillColor: "url(#aoi-hatch)", fillOpacity: 1, interactive: false };

let map, drawnLayer, drawControl, aoiRenderer;
let selectedGeometry = null;
let selectedLabel = null;
// Which reference layer (jordstykker/kloakomrader/lokalplanomrader) the
// current selection came from, and the exact Leaflet sub-layer clicked --
// replaces the old Kloakoplande-specific navn1201 matching, since the
// three sources don't share a common natural key. Object identity on
// selectedLeafletLayer is what "click the same shape again to deselect"
// and the highlight styling key off now, not a property match.
let selectedLayerKey = null;
let selectedLeafletLayer = null;
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
//
// That separate renderer is exactly why bringToFront()/insertion order
// alone can never put the AOI where it's supposed to sit relative to the
// canvas-drawn result layers: bringToFront() only reorders layers WITHIN
// one renderer, and the two renderers' root elements (an <svg>, a
// <canvas>) get their OWN CSS z-index from Leaflet directly, which
// overrides plain DOM order. Confirmed by testing, not assumed -- the
// AOI's <svg> was landing on z-index 200 against the shared canvas's
// 100, so the hatch rendered on top of Bluespot/streams regardless of
// any bringToFront() call on either side.
//
// The fix is a dedicated pane, explicitly below Leaflet's default
// overlayPane (z-index 400) where every canvas-rendered layer lives --
// this pins the AOI beneath ALL of them, permanently, by an explicit
// number rather than by hoping creation order happens to come out right.
function initAoiHatchPattern() {
  map.createPane("aoiPane");
  map.getPane("aoiPane").style.zIndex = 350;
  aoiRenderer = L.svg({ pane: "aoiPane" }).addTo(map);
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

// Builds this feature's sidebar label from whichever properties that
// source actually carries -- the three WFS sources don't share a schema,
// so there's no single field name to reach for the way navn1201 used to
// be the one true key for the old Kloakoplande-only design.
function referenceFeatureLabel(layerKey, feature) {
  const p = feature.properties || {};
  if (layerKey === "jordstykker") {
    return `Jordstykke ${p.matrikelnr || "?"}, ${p.ejerlavnavn || "ukendt ejerlav"}`;
  }
  const kind = layerKey === "kloakomrader" ? "Kloakområde" : "Lokalplan";
  return p.plannavn ? `${kind} ${p.plannr} – ${p.plannavn}` : `${kind} ${p.plannr || "?"}`;
}

// Clicking the already-selected shape again deselects it, matching the
// old Kloakoplande list's behaviour -- not a one-way trip that only
// picking something else can undo.
function selectReferenceFeature(layerKey, feature, layer) {
  if (isRunning) return;
  if (layer === selectedLeafletLayer) {
    clearSelection();
    return;
  }

  clearSelection();
  layer.setStyle({ weight: 3, fillOpacity: 0.35, fillColor: REFERENCE_LAYER_STYLES[layerKey].color });
  layer.bringToFront();

  selectedGeometry = feature.geometry;
  selectedLabel = referenceFeatureLabel(layerKey, feature);
  selectedLayerKey = layerKey;
  selectedLeafletLayer = layer;
  updateSelectionUI();
}

// Also resets any reference-layer highlight -- covers both an explicit
// deselect click and switching away from "select" mode, which previously
// (with the old Kloakoplande-only design) left the last-selected plan
// area visually highlighted even after its selection no longer meant
// anything.
function clearSelection() {
  if (selectedLayerKey && referenceLayers[selectedLayerKey]) {
    referenceLayers[selectedLayerKey].resetStyle(selectedLeafletLayer);
  }
  selectedLayerKey = null;
  selectedLeafletLayer = null;
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
  document.getElementById("layers-list").classList.toggle("is-locked", locked);
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
    // Re-entering select mode -- including re-clicking the tab while
    // already on it -- always brings the plan-area list back, so
    // collapsing it after a run (see renderResults) is never a dead end.
    document.querySelector('.mode-panel[data-mode-panel="select"]').classList.remove("is-collapsed");
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

// Walks MAP_LAYER_ORDER bottom to top, re-pinning each entry to the front
// of its own renderer in turn -- after this runs, the last entry
// (Bluespot) is guaranteed topmost again. Needed after anything that
// might have disturbed the order: a legend checkbox re-adding a layer, or
// a reference layer refreshing on pan (re-adding always puts a layer on
// top again, which would otherwise bury whichever of these is supposed
// to stay above it). The "AOI" entry's bringToFront() only matters within
// its own tiny single-shape SVG renderer -- its position relative to the
// canvas is fixed by the pane z-index instead, see the comment on
// MAP_LAYER_ORDER above.
function reassertMapLayerOrder() {
  MAP_LAYER_ORDER.forEach((name) => {
    if (name === "AOI") {
      if (analyzedAreaLayer) analyzedAreaLayer.bringToFront();
      return;
    }
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
        reassertMapLayerOrder();
      } else {
        map.removeLayer(layer);
      }
    });
    legend.appendChild(li);
  }

  // One loop over the one list (see MAP_LAYER_ORDER) -- for every entry
  // except "AOI" this just means calling addResultLayer in that order;
  // "AOI" is the one entry built differently, since it isn't one of the
  // named result layers the backend returns.
  MAP_LAYER_ORDER.forEach((name) => {
    if (name === "AOI") {
      if (geometry) {
        analyzedAreaLayer = L.geoJSON(
          { type: "Feature", geometry, properties: {} },
          { style: () => ANALYZED_AREA_STYLE, renderer: aoiRenderer }
        ).addTo(map);
      }
      return;
    }
    addResultLayer(name);
  });

  // Reflect the persisted scale in the slider itself, in case it was
  // adjusted on a previous run -- it shouldn't silently reset to 1 here.
  document.getElementById("stream-width-slider").value = streamWidthScale;
  document.getElementById("stream-width-value").textContent = `${streamWidthScale.toFixed(1)}×`;

  document.getElementById("results-panel").classList.remove("is-hidden");

  // Once there's a preselected area's results to look at, the layer
  // toggle list has done its job and just eats vertical space --
  // collapsing it is what gives the stream-width slider real presence
  // without scrolling. Draw mode already gets this for free (its panel is
  // just two buttons, far shorter to begin with), which is why only
  // select mode needs the collapse triggered explicitly.
  if (currentMode === "select") {
    document.querySelector('.mode-panel[data-mode-panel="select"]').classList.add("is-collapsed");
  }

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

  document.querySelectorAll(".mode-tab").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });

  document.getElementById("run-btn").addEventListener("click", runAnalysis);

  document.getElementById("stream-width-slider").addEventListener("input", (e) => {
    streamWidthScale = Number(e.target.value);
    document.getElementById("stream-width-value").textContent = `${streamWidthScale.toFixed(1)}×`;
    applyStreamWidthScale();
  });

  initExportButton();
  initFeedbackLink();
  initSelectableLayers();
}

// --- Selectable reference layers (Jordstykker, Kloakområder, Lokalplanområder) ---
//
// These ARE the area picker now -- there is no separate static list.
// Originally Kloakoplande.gpkg (a bundled local file, VL002/VL003/...)
// drove selection while these three existed only as passive background
// context; that meant two disconnected things both called "kloakområde"
// on screen at once. Replaced entirely: toggle a layer on, click a shape
// on the map, that's the analysis area -- one live, authoritative source
// per layer type instead of a local file duplicating what Plandata
// already publishes.
//
// Fetched by whatever's currently on screen, not the whole kommune --
// confirmed necessary by testing before building this. Vejle alone has
// 2,338 kloakområder and 857 lokalplaner, the unfiltered lokalplan
// response running ~6.8MB (each feature carries ~250 attribute fields we
// don't use). Viewport scoping plus PROPERTYNAME trimming keeps each
// fetch small and fast regardless of kommune size -- and, unlike the old
// design, a click-to-select model doesn't need every feature loaded at
// once anyway, only whatever's currently visible.
//
// Two services, two real coordinate-order gotchas -- both found by
// testing, not assumed, so documented here rather than left to bite
// again:
//   - Dataforsyningen's jordstykker `polygon` param wants ordinary
//     [lon,lat] pairs, same order as GeoJSON.
//   - Plandata's WFS native `BBOX` param wants [lat,lon,lat,lon] --
//     backwards from GeoJSON. A [lon,lat] bbox doesn't error, it just
//     silently matches zero features, which is a much easier mistake to
//     miss than an outright failure.
//   - Plandata's geometry column is literally named "geometri" (not the
//     usual geom/the_geom) -- and PROPERTYNAME must list it explicitly,
//     since GeoServer only includes attributes you name once that
//     parameter is present at all; leaving it out silently drops the
//     geometry too, not just the extra attributes.

const REFERENCE_LAYER_STYLES = {
  jordstykker: { color: "#94a3b8", weight: 1, fillOpacity: 0.03 },
  kloakomrader: { color: "#b45309", weight: 1.4, dashArray: "4,3", fillOpacity: 0.05 },
  lokalplanomrader: { color: "#7e22ce", weight: 1.4, dashArray: "4,3", fillOpacity: 0.05 },
};

// A cap, not a hard limit the UI hides -- if a fetch comes back at exactly
// this count, there's very likely more just outside it, and the note
// under that layer's checkbox says so rather than silently truncating.
const REFERENCE_LAYER_MAX_FEATURES = 300;

let referenceLayers = { jordstykker: null, kloakomrader: null, lokalplanomrader: null };
// Bumped on every fetch kicked off for a given layer, so a slow older
// request can recognize it's stale (a newer pan already superseded it)
// and discard its result instead of clobbering what's now on screen.
let referenceLayerRequestId = { jordstykker: 0, kloakomrader: 0, lokalplanomrader: 0 };

function boundsToLonLatRing(bounds) {
  const sw = bounds.getSouthWest();
  const ne = bounds.getNorthEast();
  return [[
    [sw.lng, sw.lat], [ne.lng, sw.lat], [ne.lng, ne.lat], [sw.lng, ne.lat], [sw.lng, sw.lat],
  ]];
}

async function fetchJordstykker(bounds) {
  const ring = boundsToLonLatRing(bounds);
  const url = "https://api.dataforsyningen.dk/jordstykker?" + new URLSearchParams({
    format: "geojson",
    per_side: String(REFERENCE_LAYER_MAX_FEATURES),
    polygon: JSON.stringify(ring),
  });
  const res = await fetch(url);
  if (!res.ok) throw new Error(`jordstykker: ${res.status}`);
  return res.json();
}

async function fetchPlandataLayer(typeName, bounds) {
  const sw = bounds.getSouthWest();
  const ne = bounds.getNorthEast();
  const bbox = `${sw.lat},${sw.lng},${ne.lat},${ne.lng},urn:ogc:def:crs:EPSG::4326`;
  const url = "https://geoserver.plandata.dk/geoserver/wfs?" + new URLSearchParams({
    service: "WFS", version: "2.0.0", request: "GetFeature",
    typeName: `pdk:${typeName}`, outputFormat: "application/json",
    srsName: "EPSG:4326", BBOX: bbox,
    PROPERTYNAME: "geometri,plannavn,plannr",
    count: String(REFERENCE_LAYER_MAX_FEATURES),
  });
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${typeName}: ${res.status}`);
  return res.json();
}

const REFERENCE_LAYER_FETCHERS = {
  jordstykker: (bounds) => fetchJordstykker(bounds),
  kloakomrader: (bounds) => fetchPlandataLayer("theme_pdk_kloakopland_vedtaget", bounds),
  lokalplanomrader: (bounds) => fetchPlandataLayer("theme_pdk_lokalplan_vedtaget", bounds),
};

function setReferenceLayerNote(key, text) {
  const el = document.querySelector(`[data-note="${key}"]`);
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("is-hidden", !text);
}

async function refreshReferenceLayer(key) {
  const checkbox = document.querySelector(`input[data-layer="${key}"]`);
  if (!checkbox || !checkbox.checked) return;

  // Never swap out the layer holding the current selection out from under
  // the user mid-pan -- they've committed to a shape, so the ground it's
  // drawn on should stay put (and stay highlighted) until they explicitly
  // clear it, even though every OTHER toggled-on layer keeps refreshing
  // live as normal.
  if (key === selectedLayerKey) return;

  const requestId = ++referenceLayerRequestId[key];
  try {
    const geojson = await REFERENCE_LAYER_FETCHERS[key](map.getBounds());
    if (requestId !== referenceLayerRequestId[key]) return; // superseded by a later pan
    if (!checkbox.checked) return; // toggled off while the fetch was in flight
    if (key === selectedLayerKey) return; // got selected while the fetch was in flight

    if (referenceLayers[key]) map.removeLayer(referenceLayers[key]);
    const layer = L.geoJSON(geojson, {
      style: () => REFERENCE_LAYER_STYLES[key],
      onEachFeature: (feature, featureLayer) => {
        featureLayer.on("click", () => selectReferenceFeature(key, feature, featureLayer));
      },
    });
    layer.addTo(map);
    referenceLayers[key] = layer;

    // Re-adding a layer always draws it on top of whatever's already on
    // the map -- harmless before an analysis has run, but after one has,
    // this would otherwise silently bury Bluespot/streams under whichever
    // reference layer next refreshes on a pan. A no-op when there are no
    // results yet (see the guards inside reassertMapLayerOrder).
    reassertMapLayerOrder();

    const count = geojson.features ? geojson.features.length : 0;
    setReferenceLayerNote(
      key,
      count >= REFERENCE_LAYER_MAX_FEATURES ? "Kun de første i udsnittet -- zoom ind for flere." : ""
    );
  } catch (err) {
    if (requestId !== referenceLayerRequestId[key]) return;
    setReferenceLayerNote(key, "Kunne ikke hente laget.");
  }
}

function initSelectableLayers() {
  document.querySelectorAll("#layers-list input[type=\"checkbox\"]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const key = checkbox.dataset.layer;
      if (checkbox.checked) {
        refreshReferenceLayer(key);
      } else {
        // Hiding the layer that the current selection came from: the
        // selected shape is now invisible, so the selection itself no
        // longer means anything either.
        if (key === selectedLayerKey) clearSelection();
        if (referenceLayers[key]) {
          map.removeLayer(referenceLayers[key]);
          referenceLayers[key] = null;
        }
        setReferenceLayerNote(key, "");
      }
    });
  });

  // Debounced: refetches whichever reference layers are active any time
  // the visible area changes, rather than on every intermediate pan frame.
  let moveTimer = null;
  map.on("moveend", () => {
    clearTimeout(moveTimer);
    moveTimer = setTimeout(() => {
      Object.keys(REFERENCE_LAYER_FETCHERS).forEach(refreshReferenceLayer);
    }, 400);
  });
}

// The feedback address is a <button>, not a plain mailto: <a>, and
// assembled here rather than written out anywhere as a literal string --
// see the comment on .feedback-link in style.css. This defeats a plain
// regex scraper reading the shipped HTML/JS files (still the common case
// for address-harvesting bots, since running a full browser per page is
// expensive at scale) but not a bot that executes JavaScript and
// simulates a real click: nothing client-side can stop that, since the
// browser itself has to see the real address to open a mail client. It
// raises the bar, it doesn't remove it.
function initFeedbackLink() {
  const btn = document.getElementById("feedback-link");
  if (!btn) return;
  const user = ["a", "l", "e", "x", "a", "n", "d", "e", "r"].join("");
  const domain = ["a", "d", "i", "y", "a", "s", "a", ".", "d", "k"].join("");
  btn.addEventListener("click", () => {
    const subject = encodeURIComponent("Feedback: Oplands-screener");
    window.location.href = `mailto:${user}@${domain}?subject=${subject}`;
  });
}

// Export isn't built yet -- this just states, in writing, what the button
// will eventually do, rather than leaving a mystery icon with no
// explanation. Toggling on click (not hover) matches how every other
// dismissible element in this UI behaves (the toast, the status card),
// and closes the same ways a user would expect: clicking the button
// again, clicking anywhere else, or Escape.
function initExportButton() {
  const btn = document.getElementById("export-btn");
  const popover = document.getElementById("export-popover");

  function setOpen(open) {
    popover.classList.toggle("is-hidden", !open);
    btn.classList.toggle("is-active", open);
    btn.setAttribute("aria-expanded", open);
  }

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(popover.classList.contains("is-hidden"));
  });

  document.addEventListener("click", (e) => {
    if (!popover.classList.contains("is-hidden") && !popover.contains(e.target)) {
      setOpen(false);
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setOpen(false);
  });
}

init();
