const state = {
  points: [],
  outbound: [],
  inbound: [],
  direction: "outbound",
  progress: 0,
  playing: false,
  speed: 10,
  lastFrame: null,
  lastUiUpdate: null,
  backend: {
    available: false,
    device: null,
    playback: { state: "idle" },
    error: "",
    mode: "preview"
  },
  physicalLocation: {
    status: "idle",
    coords: null,
    error: "",
    watchId: null
  },
  viewer: null,
  mapEntities: {}
};

const fmt = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 5,
  maximumFractionDigits: 5
});
const pad = number => String(number).padStart(2, "0");
const durationText = seconds => `${pad(Math.floor(seconds / 60))}:${pad(Math.floor(seconds % 60))}`;
const destinationName = () => state.direction === "outbound" ? "L2" : "L1";
const originName = () => state.direction === "outbound" ? "L1" : "L2";
const routeId = () => state.direction === "outbound" ? "l1-to-l2" : "l2-to-l1";

async function loadRoute() {
  try {
    const response = await fetch("route_final.gpx");
    if (!response.ok) throw new Error(`GPX request failed (${response.status})`);

    const xml = await response.text();
    const doc = new DOMParser().parseFromString(xml, "application/xml");
    if (doc.querySelector("parsererror")) throw new Error("The GPX file could not be parsed.");

    state.points = [...doc.querySelectorAll("wpt")].map((node, index) => ({
      index,
      lat: Number(node.getAttribute("lat")),
      lon: Number(node.getAttribute("lon")),
      name: node.querySelector("name")?.textContent.trim() || "",
      time: new Date(node.querySelector("time")?.textContent)
    })).filter(point =>
      Number.isFinite(point.lat) &&
      Number.isFinite(point.lon) &&
      !Number.isNaN(point.time.getTime())
    );

    const l2Index = state.points.findIndex(point => point.name.trim() === "L2");
    if (l2Index <= 0 || l2Index >= state.points.length - 1) {
      throw new Error("The route needs a named L2 waypoint between its outbound and return legs.");
    }

    state.outbound = state.points.slice(0, l2Index + 1);
    state.inbound = state.points.slice(l2Index);
    renderShell();
    initCesiumMap();
    updateLiveState();
    initBackend();
    requestAnimationFrame(tick);
  } catch (error) {
    showFatalError(error);
  }
}

function route() {
  return state.direction === "outbound" ? state.outbound : state.inbound;
}

function elapsedRouteSeconds() {
  const points = route();
  if (points.length < 2) return 0;
  return Math.max(0, (points.at(-1).time - points[0].time) / 1000);
}

function interpolatedPoint() {
  const points = route();
  if (!points.length) return { lat: 0, lon: 0 };

  const target = points[0].time.getTime() + elapsedRouteSeconds() * 1000 * state.progress;
  const nextIndex = points.findIndex(point => point.time.getTime() >= target);
  if (nextIndex <= 0) return points[0];
  if (nextIndex < 0) return points.at(-1);

  const a = points[nextIndex - 1];
  const b = points[nextIndex];
  const span = b.time - a.time;
  const local = span > 0 ? (target - a.time.getTime()) / span : 1;
  return {
    lat: a.lat + (b.lat - a.lat) * local,
    lon: a.lon + (b.lon - a.lon) * local
  };
}

function completedRoutePoints() {
  const points = route();
  if (!points.length) return [];

  const current = interpolatedPoint();
  const target = points[0].time.getTime() + elapsedRouteSeconds() * 1000 * state.progress;
  const completed = points.filter(point => point.time.getTime() < target);
  return [points[0], ...completed.slice(1), current];
}

function renderShell() {
  document.getElementById("app").innerHTML = `
    <section class="app-shell">
      <main class="mission-main">
        <header class="mission-header">
          <div class="product-mark">
            <span class="product-icon" aria-hidden="true"><i></i></span>
            <div><strong>ROUTE CONSOLE</strong><span>PHYSICAL IPHONE SIMULATION</span></div>
          </div>
          <div class="mission-clocks connection-stack">
            <span class="status-pill connection-pill"><i class="status-dot"></i><span data-live="connection-label">Preview only</span></span>
            <span class="status-pill"><i class="status-dot"></i><span data-live="status">Ready to preview</span></span>
          </div>
        </header>

        <div class="mission-content">
          <div class="operations-grid">
            <aside class="route-panel panel">
              <header class="panel-header"><span>ROUTE LEGS</span><b>2</b></header>
              <section class="device-card" data-live-card="device">
                <div class="device-card-top">
                  <span class="device-indicator"></span>
                  <div>
                    <p>DEVICE LINK</p>
                    <strong data-live="device-name">Backend offline</strong>
                  </div>
                </div>
                <div class="device-grid">
                  <span><small>MODEL</small><b data-live="device-model">—</b></span>
                  <span><small>IOS</small><b data-live="device-ios">—</b></span>
                </div>
                <em data-live="device-detail">Open through the local backend to control the phone.</em>
              </section>
              <div class="route-list">
                <button class="route-card selected" data-direction="outbound">
                  <span class="route-state"></span>
                  <span class="route-copy"><strong>L1 → L2</strong><small>Outbound · 20 min</small></span>
                  <span class="route-count mono">${state.outbound.length}</span>
                </button>
                <button class="route-card" data-direction="inbound">
                  <span class="route-state"></span>
                  <span class="route-copy"><strong>L2 → L1</strong><small>Return · 20 min</small></span>
                  <span class="route-count mono">${state.inbound.length}</span>
                </button>
              </div>

              <div class="coordinate-readout">
                <p>SIMULATED POSITION</p>
                <div><span>LATITUDE</span><strong class="mono" data-live="latitude">—</strong></div>
                <div><span>LONGITUDE</span><strong class="mono" data-live="longitude">—</strong></div>
              </div>

              <div class="coordinate-readout physical-readout">
                <p>MAC PHYSICAL LOCATION</p>
                <div><span>LATITUDE</span><strong class="mono" data-live="physical-latitude">—</strong></div>
                <div><span>LONGITUDE</span><strong class="mono" data-live="physical-longitude">—</strong></div>
                <button class="secondary location-button" data-action="physical-location"><span data-live="physical-location-label">SHOW MARKER</span></button>
                <em data-live="physical-location-note">Uses browser Location Services on this Mac, not simulated iPhone GPS.</em>
              </div>

            </aside>

            <section class="map-card panel" aria-label="Interactive route map">
              <header class="map-titlebar">
                <span>CESIUM // WGS84</span>
                <b><span data-live="point-count">${route().length}</span> TRACKED</b>
              </header>
              <div id="cesiumMap"></div>
              <div class="map-actions">
                <button class="map-button" data-action="recenter" aria-label="Recenter route"><span aria-hidden="true">⌖</span> RECENTER</button>
              </div>
              <div class="map-legend">
                <span><i class="legend-dot current"></i>Simulated</span>
                <span><i class="legend-dot physical"></i>Mac physical</span>
                <span><i class="legend-line route"></i>Route</span>
                <span><i class="legend-line traveled"></i>Traveled</span>
              </div>
              <div class="map-error" data-live="map-error" hidden></div>
            </section>

            <aside class="control-panel panel">
              <header class="panel-header">
                <span>PLAYBACK CONTROL</span>
                <b class="control-state"><i class="status-dot"></i><span data-live="control-state">STANDBY</span></b>
              </header>

              <section class="control-section">
                <p>ACTIVE JOURNEY</p>
                <div class="journey-display">
                  <strong data-live="origin">L1</strong>
                  <span><i></i></span>
                  <strong data-live="destination">L2</strong>
                </div>
              </section>

              <section class="control-section">
                <p data-live="transport-title">PREVIEW TRANSPORT</p>
                <div class="button-row">
                  <button class="primary" data-action="toggle"><span class="play-icon" aria-hidden="true">▶</span><span data-live="toggle-label">Start preview</span></button>
                  <button class="secondary danger" data-action="stop">■ STOP</button>
                </div>
                <button class="secondary clear-button" data-action="clear">CLEAR LOCATION</button>
                <p class="backend-note" data-live="backend-note">Preview mode</p>
              </section>

              <section class="control-section">
                <div class="progress-label"><span>PROGRESS</span><strong class="mono" data-live="progress-percent">0%</strong></div>
                <div class="progress" role="progressbar" aria-label="Route progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
                  <div data-live="progress-bar"></div>
                </div>
                <div class="time-pair">
                  <span><small>ELAPSED</small><strong class="mono" data-live="elapsed">00:00</strong></span>
                  <span><small>REMAINING</small><strong class="mono" data-live="remaining">20:00</strong></span>
                </div>
              </section>

              <section class="control-section">
                <p>PREVIEW RATE</p>
                <div class="segmented speed-selector">
                  ${[1, 10, 30, 60].map(speed =>
                    `<button data-speed="${speed}" class="${state.speed === speed ? "selected" : ""}">${speed}×</button>`
                  ).join("")}
                </div>
              </section>

            </aside>
          </div>
        </div>
      </main>
    </section>`;

  bindControls();
}

function bindControls() {
  document.querySelectorAll("[data-direction]").forEach(button => {
    button.addEventListener("click", () => setDirection(button.dataset.direction));
  });

  document.querySelectorAll("[data-speed]").forEach(button => {
    button.addEventListener("click", () => {
      state.speed = Number(button.dataset.speed);
      updateLiveState();
    });
  });

  document.querySelector('[data-action="toggle"]').addEventListener("click", async () => {
    if (state.backend.available) {
      await toggleDevicePlayback();
      return;
    }
    if (state.progress >= 1) state.progress = 0;
    state.playing = !state.playing;
    state.lastFrame = null;
    updateLiveState();
  });

  document.querySelector('[data-action="stop"]').addEventListener("click", async () => {
    if (state.backend.available) {
      await stopDevicePlayback();
      return;
    }
    state.playing = false;
    state.progress = 0;
    state.lastFrame = null;
    updateLiveState();
  });

  document.querySelector('[data-action="clear"]').addEventListener("click", clearDeviceLocation);
  document.querySelector('[data-action="recenter"]').addEventListener("click", () => frameRoute(true));
  document.querySelector('[data-action="physical-location"]').addEventListener("click", togglePhysicalLocationMarker);
}

function setDirection(direction) {
  if (state.direction === direction) return;
  state.direction = direction;
  state.playing = false;
  state.progress = 0;
  state.lastFrame = null;
  syncMapRoute();
  frameRoute(true);
  updateLiveState();
}

async function initBackend() {
  await refreshBackendStatus();
  window.setInterval(refreshBackendStatus, 1000);
}

async function apiRequest(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

async function refreshBackendStatus() {
  try {
    const payload = await apiRequest("/api/status");
    state.backend.available = true;
    state.backend.device = parseDevice(payload.device);
    state.backend.error = "";
    state.backend.playback = payload.playback || { state: "idle" };
    syncBackendPlayback();
  } catch (error) {
    state.backend.available = false;
    state.backend.device = null;
    state.backend.error = "";
    state.backend.playback = { state: "idle" };
  }
  updateLiveState();
}

function parseDevice(report) {
  if (!report || !report.device_probe_ok || !report.device_probe_output) return null;
  try {
    const devices = JSON.parse(report.device_probe_output);
    return Array.isArray(devices) && devices.length ? devices[0] : null;
  } catch (error) {
    return null;
  }
}

function syncBackendPlayback() {
  const playback = state.backend.playback;
  if (playback.routeId === "l1-to-l2" && state.direction !== "outbound") {
    state.direction = "outbound";
    syncMapRoute();
    frameRoute(true);
  }
  if (playback.routeId === "l2-to-l1" && state.direction !== "inbound") {
    state.direction = "inbound";
    syncMapRoute();
    frameRoute(true);
  }
  if (playback.state === "playing" || playback.state === "paused") {
    state.backend.mode = "device";
    state.progress = Math.min(1, Math.max(0, playback.progress || 0));
    state.playing = playback.state === "playing";
    state.lastFrame = null;
  } else if (state.backend.mode === "device") {
    state.playing = false;
    state.lastFrame = null;
  }
}

async function toggleDevicePlayback() {
  try {
    const playback = state.backend.playback;
    if (playback.state === "playing") {
      state.backend.playback = await apiRequest("/api/playback/pause", { method: "POST" });
    } else if (playback.state === "paused") {
      state.backend.playback = await apiRequest("/api/playback/resume", { method: "POST" });
    } else {
      state.progress = 0;
      state.backend.mode = "device";
      state.backend.playback = await apiRequest(`/api/routes/${routeId()}/start`, { method: "POST" });
    }
    syncBackendPlayback();
  } catch (error) {
    state.backend.error = error.message;
  }
  updateLiveState();
}

async function stopDevicePlayback() {
  try {
    state.backend.playback = await apiRequest("/api/playback/stop", { method: "POST" });
    state.backend.mode = "preview";
    state.playing = false;
    state.progress = 0;
  } catch (error) {
    state.backend.error = error.message;
  }
  updateLiveState();
}

async function clearDeviceLocation() {
  try {
    await apiRequest("/api/location/clear", { method: "POST" });
    state.backend.error = "";
  } catch (error) {
    state.backend.error = error.message;
  }
  updateLiveState();
}

function initCesiumMap() {
  if (!window.Cesium) {
    showMapError("Cesium could not load. Check your network connection and refresh.");
    return;
  }

  try {
    state.viewer = new Cesium.Viewer("cesiumMap", {
      animation: false,
      baseLayer: new Cesium.ImageryLayer(new Cesium.OpenStreetMapImageryProvider({
        url: "https://tile.openstreetmap.org/"
      })),
      baseLayerPicker: false,
      fullscreenButton: false,
      geocoder: false,
      homeButton: false,
      infoBox: false,
      navigationHelpButton: false,
      sceneMode: Cesium.SceneMode.SCENE2D,
      sceneModePicker: false,
      selectionIndicator: false,
      shouldAnimate: false,
      timeline: false,
      requestRenderMode: true,
      maximumRenderTimeChange: Infinity
    });

    state.viewer.scene.backgroundColor = Cesium.Color.fromCssColorString("#101820");
    state.viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString("#17212a");
    state.viewer.scene.screenSpaceCameraController.minimumZoomDistance = 150;
    state.viewer.scene.screenSpaceCameraController.maximumZoomDistance = 25000000;
    syncMapRoute();
    frameRoute(false);
  } catch (error) {
    console.error(error);
    showMapError("The map could not start. Try refreshing the page.");
  }
}

function syncMapRoute() {
  const viewer = state.viewer;
  if (!viewer || viewer.isDestroyed()) return;

  Object.values(state.mapEntities).forEach(entity => viewer.entities.remove(entity));
  state.mapEntities = {};

  const points = route();
  const routePositions = degreesArray(points);
  const start = points[0];
  const end = points.at(-1);

  state.mapEntities.routeShadow = viewer.entities.add({
    polyline: {
      positions: routePositions,
      width: 10,
      material: Cesium.Color.fromCssColorString("#081018").withAlpha(0.72),
      clampToGround: true
    }
  });
  state.mapEntities.route = viewer.entities.add({
    polyline: {
      positions: routePositions,
      width: 5,
      material: Cesium.Color.fromCssColorString("#78a8ff"),
      clampToGround: true
    }
  });
  state.mapEntities.progress = viewer.entities.add({
    polyline: {
      positions: degreesArray(completedRoutePoints()),
      width: 6,
      material: Cesium.Color.fromCssColorString("#8cffb2"),
      clampToGround: true
    }
  });
  state.mapEntities.start = addEndpoint(start, originName(), "#8cffb2");
  state.mapEntities.end = addEndpoint(end, destinationName(), "#78a8ff");
  state.mapEntities.current = viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(start.lon, start.lat),
    point: {
      pixelSize: 15,
      color: Cesium.Color.WHITE,
      outlineColor: Cesium.Color.fromCssColorString("#111820"),
      outlineWidth: 4,
      disableDepthTestDistance: Number.POSITIVE_INFINITY
    }
  });
  syncPhysicalLocationMarker();
  viewer.scene.requestRender();
}

function syncPhysicalLocationMarker() {
  const viewer = state.viewer;
  if (!viewer || viewer.isDestroyed()) return;

  if (!state.physicalLocation.coords) {
    if (state.mapEntities.physicalLocation) {
      viewer.entities.remove(state.mapEntities.physicalLocation);
      delete state.mapEntities.physicalLocation;
      viewer.scene.requestRender();
    }
    return;
  }

  const { lat, lon, accuracy } = state.physicalLocation.coords;
  const position = Cesium.Cartesian3.fromDegrees(lon, lat);
  const labelText = `Mac physical${Number.isFinite(accuracy) ? ` ±${Math.round(accuracy)}m` : ""}`;
  if (!state.mapEntities.physicalLocation) {
    state.mapEntities.physicalLocation = viewer.entities.add({
      position,
      point: {
        pixelSize: 15,
        color: Cesium.Color.fromCssColorString("#f2c14e"),
        outlineColor: Cesium.Color.fromCssColorString("#111820"),
        outlineWidth: 4,
        disableDepthTestDistance: Number.POSITIVE_INFINITY
      },
      label: {
        text: labelText,
        font: "700 12px -apple-system, BlinkMacSystemFont, sans-serif",
        fillColor: Cesium.Color.WHITE,
        showBackground: true,
        backgroundColor: Cesium.Color.fromCssColorString("#10161d").withAlpha(0.88),
        backgroundPadding: new Cesium.Cartesian2(9, 6),
        pixelOffset: new Cesium.Cartesian2(0, -30),
        disableDepthTestDistance: Number.POSITIVE_INFINITY
      }
    });
  } else {
    state.mapEntities.physicalLocation.position = position;
    state.mapEntities.physicalLocation.label.text = labelText;
  }
  viewer.scene.requestRender();
}

function togglePhysicalLocationMarker() {
  if (state.physicalLocation.watchId !== null) {
    navigator.geolocation.clearWatch(state.physicalLocation.watchId);
    state.physicalLocation.watchId = null;
    state.physicalLocation.status = "idle";
    state.physicalLocation.coords = null;
    state.physicalLocation.error = "";
    syncPhysicalLocationMarker();
    updateLiveState();
    return;
  }

  if (!navigator.geolocation) {
    state.physicalLocation.status = "error";
    state.physicalLocation.error = "Browser Location Services are unavailable.";
    updateLiveState();
    return;
  }

  state.physicalLocation.status = "requesting";
  state.physicalLocation.error = "";
  updateLiveState();
  state.physicalLocation.watchId = navigator.geolocation.watchPosition(
    position => {
      state.physicalLocation.status = "watching";
      state.physicalLocation.coords = {
        lat: position.coords.latitude,
        lon: position.coords.longitude,
        accuracy: position.coords.accuracy,
        timestamp: position.timestamp
      };
      state.physicalLocation.error = "";
      syncPhysicalLocationMarker();
      updateLiveState();
    },
    error => {
      state.physicalLocation.status = "error";
      state.physicalLocation.error = locationErrorMessage(error);
      syncPhysicalLocationMarker();
      updateLiveState();
    },
    { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 }
  );
}

function locationErrorMessage(error) {
  if (error.code === error.PERMISSION_DENIED) return "Location permission was denied for this browser.";
  if (error.code === error.POSITION_UNAVAILABLE) return "This Mac's physical location is unavailable.";
  if (error.code === error.TIMEOUT) return "Timed out while asking macOS for location.";
  return error.message || "Could not read this Mac's physical location.";
}

function addEndpoint(point, label, color) {
  return state.viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(point.lon, point.lat),
    point: {
      pixelSize: 12,
      color: Cesium.Color.fromCssColorString(color),
      outlineColor: Cesium.Color.fromCssColorString("#111820"),
      outlineWidth: 3,
      disableDepthTestDistance: Number.POSITIVE_INFINITY
    },
    label: {
      text: label,
      font: "700 13px -apple-system, BlinkMacSystemFont, sans-serif",
      fillColor: Cesium.Color.WHITE,
      showBackground: true,
      backgroundColor: Cesium.Color.fromCssColorString("#10161d").withAlpha(0.88),
      backgroundPadding: new Cesium.Cartesian2(9, 6),
      pixelOffset: new Cesium.Cartesian2(0, -28),
      disableDepthTestDistance: Number.POSITIVE_INFINITY
    }
  });
}

function degreesArray(points) {
  if (!points.length) return [];
  const safePoints = points.length === 1 ? [points[0], points[0]] : points;
  return Cesium.Cartesian3.fromDegreesArray(safePoints.flatMap(point => [point.lon, point.lat]));
}

function frameRoute(animated) {
  const viewer = state.viewer;
  if (!viewer || viewer.isDestroyed()) return;

  const points = route();
  const minLat = Math.min(...points.map(point => point.lat));
  const maxLat = Math.max(...points.map(point => point.lat));
  const minLon = Math.min(...points.map(point => point.lon));
  const maxLon = Math.max(...points.map(point => point.lon));
  const latPad = Math.max((maxLat - minLat) * 0.13, 0.002);
  const lonPad = Math.max((maxLon - minLon) * 0.13, 0.002);
  const destination = Cesium.Rectangle.fromDegrees(
    minLon - lonPad,
    minLat - latPad,
    maxLon + lonPad,
    maxLat + latPad
  );

  if (animated) {
    viewer.camera.flyTo({ destination, duration: 0.7 });
  } else {
    viewer.camera.setView({ destination });
  }
  viewer.scene.requestRender();
}

function updateLiveState() {
  if (!state.points.length) return;

  const total = elapsedRouteSeconds();
  const elapsed = total * state.progress;
  const current = interpolatedPoint();
  const percent = Math.round(state.progress * 100);
  const backendPlayback = state.backend.playback || { state: "idle" };
  const device = state.backend.device;
  const deviceOnline = state.backend.available && !!device;
  const status = state.backend.available && backendPlayback.state === "playing"
    ? `Phone traveling to ${destinationName()}`
    : state.backend.available && backendPlayback.state === "paused"
      ? "Phone route paused"
      : state.playing
        ? `Previewing to ${destinationName()}`
        : state.progress >= 1
          ? `Arrived at ${destinationName()}`
          : state.progress > 0
            ? "Paused"
            : state.backend.available
              ? "Phone backend ready"
              : "Ready to preview";

  setText("status", status);
  setText("connection-label", deviceOnline
    ? "Backend + iPhone connected"
    : state.backend.available
      ? "Backend online / phone not detected"
      : "Preview only");
  setText("origin", originName());
  setText("destination", destinationName());
  setText("toggle-label", state.backend.available
    ? backendPlayback.state === "playing"
      ? "Pause phone"
      : backendPlayback.state === "paused"
        ? "Resume phone"
        : "Start on phone"
    : state.playing ? "Pause" : state.progress > 0 && state.progress < 1 ? "Resume" : "Start preview");
  setText("progress-text", `${percent}% complete`);
  setText("progress-percent", `${percent}%`);
  setText("point-count", `${route().length}`);
  setText("elapsed", durationText(elapsed));
  setText("remaining", durationText(Math.max(0, total - elapsed)));
  setText("latitude", fmt.format(current.lat));
  setText("longitude", fmt.format(current.lon));
  setText("physical-latitude", state.physicalLocation.coords ? fmt.format(state.physicalLocation.coords.lat) : "—");
  setText("physical-longitude", state.physicalLocation.coords ? fmt.format(state.physicalLocation.coords.lon) : "—");
  setText("physical-location-label", state.physicalLocation.watchId !== null ? "HIDE MARKER" : "SHOW MARKER");
  setText("physical-location-note", state.physicalLocation.error
    ? state.physicalLocation.error
    : state.physicalLocation.status === "requesting"
      ? "Waiting for browser Location Services permission…"
      : state.physicalLocation.coords
        ? `Browser Location Services · accuracy ±${Math.round(state.physicalLocation.coords.accuracy)}m`
        : "Uses browser Location Services on this Mac, not simulated iPhone GPS.");
  setText("control-state", state.backend.available
    ? backendPlayback.state === "playing"
      ? "DEVICE"
      : backendPlayback.state === "paused"
        ? "PAUSED"
        : "READY"
    : state.playing ? "RUNNING" : state.progress >= 1 ? "COMPLETE" : state.progress > 0 ? "PAUSED" : "STANDBY");
  setText("backend-note", state.backend.error
    ? state.backend.error
    : state.backend.available
      ? deviceOnline
        ? "Device controls are live"
        : "Backend is running, but no USB iPhone is detected"
      : "Preview mode");
  setText("transport-title", state.backend.available ? "IPHONE TRANSPORT" : "PREVIEW TRANSPORT");
  setText("device-name", deviceOnline ? device.DeviceName || "Connected iPhone" : state.backend.available ? "No iPhone detected" : "Backend offline");
  setText("device-model", deviceOnline ? device.ProductType || "iPhone" : "—");
  setText("device-ios", deviceOnline ? device.ProductVersion || "—" : "—");
  setText("device-detail", deviceOnline
    ? `${device.ConnectionType || "USB"} · ${device.Identifier || "paired device"}`
    : state.backend.available
      ? "Connect and unlock the phone, then refresh status."
      : "Run ./scripts/run_frontend.sh for live phone control.");

  document.querySelectorAll(".status-dot").forEach(dot => {
    dot.classList.toggle("playing", state.playing);
    dot.classList.toggle("complete", state.progress >= 1);
  });
  document.querySelector(".play-icon").textContent = state.playing ? "Ⅱ" : "▶";
  document.querySelectorAll("[data-direction]").forEach(button =>
    button.classList.toggle("selected", button.dataset.direction === state.direction)
  );
  document.querySelectorAll("[data-speed]").forEach(button =>
    button.classList.toggle("selected", Number(button.dataset.speed) === state.speed)
  );
  document.querySelectorAll(".connection-pill, [data-live-card='device']").forEach(node => {
    node.classList.toggle("online", deviceOnline);
    node.classList.toggle("backend-only", state.backend.available && !deviceOnline);
  });

  const progress = document.querySelector(".progress");
  const bar = document.querySelector('[data-live="progress-bar"]');
  if (progress) progress.setAttribute("aria-valuenow", String(percent));
  if (bar) bar.style.width = `${state.progress * 100}%`;

  const viewer = state.viewer;
  if (viewer && !viewer.isDestroyed() && state.mapEntities.current) {
    state.mapEntities.current.position = Cesium.Cartesian3.fromDegrees(current.lon, current.lat);
    state.mapEntities.progress.polyline.positions = degreesArray(completedRoutePoints());
    viewer.scene.requestRender();
  }
}

function setText(key, value) {
  document.querySelectorAll(`[data-live="${key}"]`).forEach(node => {
    node.textContent = value;
  });
}

function showMapError(message) {
  const element = document.querySelector('[data-live="map-error"]');
  if (!element) return;
  element.textContent = message;
  element.hidden = false;
}

function showFatalError(error) {
  console.error(error);
  document.getElementById("app").innerHTML = `
    <section class="fatal-error">
      <span>!</span>
      <h1>Route console could not start</h1>
      <p>${escapeHtml(error.message || "An unexpected error occurred.")}</p>
      <button onclick="location.reload()">Try again</button>
    </section>`;
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

function tick(now) {
  if (state.playing) {
    if (state.lastFrame !== null) {
      const total = elapsedRouteSeconds();
      if (total > 0) {
        state.progress = Math.min(1, state.progress + ((now - state.lastFrame) / 1000) * state.speed / total);
      }
      if (state.progress >= 1) state.playing = false;

      if (state.lastUiUpdate === null || now - state.lastUiUpdate >= 100 || !state.playing) {
        updateLiveState();
        state.lastUiUpdate = now;
      }
    }
    state.lastFrame = now;
  } else {
    state.lastFrame = null;
    state.lastUiUpdate = null;
  }
  requestAnimationFrame(tick);
}

loadRoute();
