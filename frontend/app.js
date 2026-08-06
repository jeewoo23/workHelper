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
  previewDurationSeconds: {
    outbound: null,
    inbound: null,
    imported: null
  },
  previewRemainingInputFocused: false,
  backend: {
    available: false,
    apiVersion: 0,
    capabilities: {},
    device: null,
    deviceReport: null,
    routes: [],
    playback: { state: "idle" },
    error: "",
    mode: "preview"
  },
  physicalLocation: {
    status: "idle",
    coords: null,
    error: "",
    watchId: null,
    pendingPan: false,
    pendingBuilderTarget: null
  },
  staticLocation: {
    status: "idle",
    point: null,
    draftPoint: null,
    pickMode: false,
    error: ""
  },
  routeBuilder: {
    status: "idle",
    error: "",
    origin: null,
    destination: null,
    originLabel: "Origin",
    destinationLabel: "Destination",
    pickTarget: null,
    lastMetadata: null
  },
  trackSimulatedLocation: false,
  mapRouteGeometry: {
    completed: [],
    remaining: []
  },
  importedRoute: {
    status: "idle",
    error: "",
    metadata: null,
    points: [],
    items: [],
    deletingFilename: "",
    preparationStatus: "idle",
    preparationError: ""
  },
  viewer: null,
  mapPickHandler: null,
  mapEntities: {}
};

const fmt = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 7,
  maximumFractionDigits: 7
});
const pad = number => String(number).padStart(2, "0");
const durationText = seconds => `${pad(Math.floor(seconds / 60))}:${pad(Math.floor(seconds % 60))}`;
const destinationName = () => state.direction === "imported"
  ? state.importedRoute.metadata?.destinationLabel || "END"
  : state.direction === "outbound" ? "L2" : "L1";
const originName = () => state.direction === "imported"
  ? state.importedRoute.metadata?.originLabel || "START"
  : state.direction === "outbound" ? "L1" : "L2";
const routeId = () => {
  if (state.direction === "imported") {
    return preparedRouteForImport(state.importedRoute.metadata)?.id || null;
  }
  return backendRouteForDirection(state.direction)?.id
    || (state.direction === "outbound" ? "l1-to-l2" : "l2-to-l1");
};

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
    state.previewDurationSeconds.outbound = routeDurationFromPoints(state.outbound);
    state.previewDurationSeconds.inbound = routeDurationFromPoints(state.inbound);
    renderShell();
    initCesiumMap();
    updateLiveState();
    initBackend();
    requestAnimationFrame(tick);
  } catch (error) {
    showFatalError(error);
  }
}

function elementsByLocalName(root, name) {
  return [...root.getElementsByTagNameNS("*", name)];
}

function firstChildText(root, name) {
  return elementsByLocalName(root, name)[0]?.textContent.trim() || "";
}

function parseImportedGpx(xml) {
  if (/<!doctype|<!entity/i.test(xml)) {
    throw new Error("GPX document type and entity declarations are not allowed.");
  }
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  if (doc.querySelector("parsererror")) {
    throw new Error("The selected GPX file could not be parsed.");
  }

  const groups = [
    elementsByLocalName(doc, "trkpt"),
    elementsByLocalName(doc, "rtept"),
    elementsByLocalName(doc, "wpt")
  ];
  const nodes = groups.find(group => group.length >= 2) || [];
  if (nodes.length < 2) {
    throw new Error("The GPX route needs at least two track, route, or waypoint coordinates.");
  }

  const points = nodes.map((node, index) => {
    const lat = Number(node.getAttribute("lat"));
    const lon = Number(node.getAttribute("lon"));
    const timeText = firstChildText(node, "time");
    const time = timeText ? new Date(timeText) : null;
    if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
      throw new Error(`Point ${index + 1} has an invalid latitude.`);
    }
    if (!Number.isFinite(lon) || lon < -180 || lon > 180) {
      throw new Error(`Point ${index + 1} has an invalid longitude.`);
    }
    return {
      index,
      lat,
      lon,
      name: firstChildText(node, "name"),
      time: time && !Number.isNaN(time.getTime()) ? time : null
    };
  });

  const fullyTimed = points.every(point => point.time instanceof Date);
  const monotonic = fullyTimed && points.every((point, index) =>
    index === 0 || point.time >= points[index - 1].time
  );
  const positiveDuration = monotonic && points.at(-1).time > points[0].time;
  if (!positiveDuration) assignPreviewTimes(points, 20 * 60);
  return points;
}

function assignPreviewTimes(points, durationSeconds) {
  const distances = [0];
  for (let index = 1; index < points.length; index += 1) {
    distances.push(distances.at(-1) + distanceMeters(points[index - 1], points[index]));
  }
  const totalDistance = distances.at(-1);
  const startedAt = Date.UTC(2026, 0, 1, 12, 0, 0);
  points.forEach((point, index) => {
    const fraction = totalDistance > 0
      ? distances[index] / totalDistance
      : index / Math.max(1, points.length - 1);
    point.time = new Date(startedAt + durationSeconds * 1000 * fraction);
  });
}

function distanceMeters(first, second) {
  const toRadians = value => value * Math.PI / 180;
  const firstLatitude = toRadians(first.lat);
  const secondLatitude = toRadians(second.lat);
  const latitudeDelta = secondLatitude - firstLatitude;
  const longitudeDelta = toRadians(second.lon - first.lon);
  const haversine = Math.sin(latitudeDelta / 2) ** 2
    + Math.cos(firstLatitude) * Math.cos(secondLatitude)
    * Math.sin(longitudeDelta / 2) ** 2;
  const clamped = Math.min(1, Math.max(0, haversine));
  return 6371000 * 2 * Math.atan2(Math.sqrt(clamped), Math.sqrt(1 - clamped));
}

async function importGpxFile(file) {
  if (!file) return;
  if (isDevicePlaybackActive()) {
    setImportError("Stop phone playback before importing another route.");
    return;
  }
  if (!state.backend.available) {
    setImportError("Start the local controller before importing a GPX file.");
    return;
  }
  if (!file.name.toLowerCase().endsWith(".gpx")) {
    setImportError("Choose a file whose name ends in .gpx.");
    return;
  }
  if (file.size > 5 * 1024 * 1024) {
    setImportError("The GPX file must be 5 MiB or smaller.");
    return;
  }

  state.importedRoute.status = "uploading";
  state.importedRoute.error = "";
  updateImportPanel();
  try {
    const content = await file.text();
    const points = parseImportedGpx(content);
    const metadata = await apiRequest("/api/routes/import-gpx", {
      method: "POST",
      body: JSON.stringify({ filename: file.name, content })
    });

    const items = state.importedRoute.items.filter(
      item => item.metadata.filename !== metadata.filename
    );
    items.unshift({ metadata, points });
    state.importedRoute = {
      status: "ready",
      error: "",
      metadata,
      points,
      items,
      deletingFilename: "",
      preparationStatus: "idle",
      preparationError: ""
    };
    state.previewDurationSeconds.imported = routeDurationFromPoints(points) || 20 * 60;
    state.direction = "imported";
    state.playing = false;
    state.progress = 0;
    state.lastFrame = null;
    syncMapRoute();
    frameRoute(true);
    updateRouteCards();
    updateLiveState();
  } catch (error) {
    setImportError(error.message || "The GPX route could not be imported.");
  } finally {
    const input = document.querySelector('[data-input="gpx-file"]');
    if (input) input.value = "";
  }
}

function setImportError(message) {
  state.importedRoute.status = "error";
  state.importedRoute.error = message;
  updateImportPanel();
}

async function refreshImportedRoutes() {
  if (!state.backend.available) {
    updateRouteCards();
    return;
  }
  try {
    const payload = await apiRequest("/api/routes/imports");
    const cached = new Map(
      state.importedRoute.items.map(item => [item.metadata.filename, item])
    );
    state.importedRoute.items = (Array.isArray(payload.imports) ? payload.imports : [])
      .map(metadata => ({
        metadata,
        points: cached.get(metadata.filename)?.points || null,
        preparedRouteId: cached.get(metadata.filename)?.preparedRouteId || null
      }));
    state.importedRoute.error = "";
  } catch (error) {
    state.importedRoute.error = error.message;
  }
  updateRouteCards();
}

async function selectImportedRoute(filename) {
  if (isDevicePlaybackActive()) {
    setImportError("Stop phone playback before previewing another route.");
    return;
  }
  const item = state.importedRoute.items.find(
    candidate => candidate.metadata.filename === filename
  );
  if (!item) return;

  state.importedRoute.status = "loading";
  state.importedRoute.error = "";
  updateImportPanel();
  try {
    if (!item.points) {
      const detail = await apiRequest(
        `/api/routes/imports/${encodeURIComponent(filename)}`
      );
      const { content, ...metadata } = detail;
      item.metadata = metadata;
      item.points = parseImportedGpx(content);
    }
    const prepared = preparedRouteForImport(item.metadata);
    if (prepared && item.preparedRouteId !== prepared.id) {
      const preview = await apiRequest(
        `/api/routes/${encodeURIComponent(prepared.id)}/preview`
      );
      const preparedPoints = playbackPointsFromPayload(preview.points);
      if (preparedPoints.length >= 2) {
        item.points = preparedPoints;
        item.preparedRouteId = prepared.id;
      }
    }
    activateImportedRoute(item);
  } catch (error) {
    setImportError(error.message || "The imported route could not be opened.");
  }
}

async function deleteImportedRoute(filename) {
  const item = state.importedRoute.items.find(
    candidate => candidate.metadata.filename === filename
  );
  if (!item || state.importedRoute.deletingFilename) return;
  if (!supportsBackendCapability("deleteImports")) {
    state.importedRoute.status = "delete-error";
    state.importedRoute.error = "Restart the local controller to enable GPX deletion.";
    updateRouteCards();
    return;
  }
  if (isDevicePlaybackActive()) {
    state.importedRoute.status = "delete-error";
    state.importedRoute.error = "Stop phone playback before deleting an imported route.";
    updateRouteCards();
    return;
  }

  const label = item.metadata.name
    || item.metadata.originalFilename
    || item.metadata.filename;
  const confirmed = window.confirm(
    `Delete “${label}” and its prepared playback track? This cannot be undone.`
  );
  if (!confirmed) return;

  state.importedRoute.status = "deleting";
  state.importedRoute.error = "";
  state.importedRoute.deletingFilename = filename;
  updateRouteCards();
  try {
    const deleted = await apiRequest(
      `/api/routes/imports/${encodeURIComponent(filename)}`,
      { method: "DELETE" }
    );
    const removedRouteIds = new Set(deleted.removedRouteIds || []);
    state.backend.routes = state.backend.routes.filter(
      routeMeta => !removedRouteIds.has(routeMeta.id)
    );
    state.importedRoute.items = state.importedRoute.items.filter(
      candidate => candidate.metadata.filename !== filename
    );
    const deletedActiveRoute = state.importedRoute.metadata?.filename === filename;
    state.importedRoute.deletingFilename = "";
    state.importedRoute.error = "";

    if (deletedActiveRoute) {
      state.importedRoute.metadata = null;
      state.importedRoute.points = [];
      state.previewDurationSeconds.imported = null;
      state.direction = "outbound";
      state.playing = false;
      state.progress = 0;
      state.lastFrame = null;
      state.importedRoute.preparationStatus = "idle";
      state.importedRoute.preparationError = "";
      state.importedRoute.status = "idle";
      syncMapRoute();
      frameRoute(true);
    } else {
      state.importedRoute.status = state.importedRoute.metadata ? "ready" : "idle";
    }
    updateRouteCards();
    updateLiveState();
  } catch (error) {
    state.importedRoute.status = "delete-error";
    state.importedRoute.error = error.message || "The imported route could not be deleted.";
    state.importedRoute.deletingFilename = "";
    updateRouteCards();
  }
}

function activateImportedRoute(item) {
  state.importedRoute.status = "ready";
  state.importedRoute.error = "";
  state.importedRoute.metadata = item.metadata;
  state.importedRoute.points = item.points;
  state.importedRoute.preparationStatus = "idle";
  state.importedRoute.preparationError = "";
  state.previewDurationSeconds.imported = preparedRouteForImport(item.metadata)?.durationSeconds
    || routeDurationFromPoints(item.points)
    || 20 * 60;
  state.direction = "imported";
  state.playing = false;
  state.progress = 0;
  state.lastFrame = null;
  syncMapRoute();
  frameRoute(true);
  updateRouteCards();
  updateLiveState();
}

async function prepareActiveImport() {
  const metadata = state.importedRoute.metadata;
  const durationInput = document.querySelector('[data-input="import-duration"]');
  const autoDurationInput = document.querySelector('[data-input="import-auto-duration"]');
  if (!metadata || !durationInput || !autoDurationInput) return;
  if (isDevicePlaybackActive()) {
    state.importedRoute.preparationStatus = "error";
    state.importedRoute.preparationError = "Stop phone playback before rebuilding a route.";
    updatePreparationPanel();
    return;
  }

  const useBestAvailableTime = autoDurationInput.checked;
  if (
    useBestAvailableTime
    && !supportsBackendCapability("nullableDuration")
  ) {
    state.importedRoute.preparationStatus = "error";
    state.importedRoute.preparationError = "Restart the local controller to enable route-aware timing.";
    updatePreparationPanel();
    return;
  }
  const durationMinutes = Number(durationInput.value);
  if (!useBestAvailableTime) {
    if (
      !Number.isFinite(durationMinutes)
      || durationMinutes < 0.2
      || durationMinutes > 1440
    ) {
      state.importedRoute.preparationStatus = "error";
      state.importedRoute.preparationError = "Duration must be between 0.2 and 1440 minutes.";
      updatePreparationPanel();
      return;
    }
  }

  state.importedRoute.preparationStatus = "preparing";
  state.importedRoute.preparationError = "";
  updatePreparationPanel();
  try {
    const prepared = await apiRequest(
      `/api/routes/imports/${encodeURIComponent(metadata.filename)}/prepare`,
      {
        method: "POST",
        body: JSON.stringify({
          durationSeconds: useBestAvailableTime ? null : durationMinutes * 60,
          timingMode: "auto",
          label: metadata.name || metadata.originalFilename || metadata.filename,
          originLabel: metadata.originLabel || "START",
          destinationLabel: metadata.destinationLabel || "END"
        })
      }
    );
    const previewPoints = playbackPointsFromPayload(prepared.previewPoints);
    const preparedRoute = { ...prepared };
    delete preparedRoute.previewPoints;
    state.backend.routes = [
      ...state.backend.routes.filter(routeMeta => routeMeta.id !== preparedRoute.id),
      preparedRoute
    ];
    if (previewPoints.length >= 2) {
      state.importedRoute.points = previewPoints;
      const item = state.importedRoute.items.find(
        candidate => candidate.metadata.filename === metadata.filename
      );
      if (item) {
        item.points = previewPoints;
        item.preparedRouteId = preparedRoute.id;
      }
      syncMapRoute();
    }
    state.previewDurationSeconds.imported = preparedRoute.durationSeconds;
    durationInput.value = formatMinutesInput(preparedRoute.durationSeconds / 60);
    state.progress = 0;
    state.playing = false;
    state.lastFrame = null;
    state.importedRoute.preparationStatus = "ready";
    state.importedRoute.preparationError = "";
    updateRouteCards();
    updateLiveState();
  } catch (error) {
    state.importedRoute.preparationStatus = "error";
    state.importedRoute.preparationError = error.message || "The route could not be prepared.";
    updatePreparationPanel();
  }
}

function playbackPointsFromPayload(points) {
  if (!Array.isArray(points)) return [];
  return points.map((point, index) => ({
    index,
    lat: Number(point.latitude),
    lon: Number(point.longitude),
    name: point.name || "",
    time: new Date(point.time)
  })).filter(point =>
    Number.isFinite(point.lat)
    && Number.isFinite(point.lon)
    && !Number.isNaN(point.time.getTime())
  );
}

function route() {
  if (state.direction === "imported") return state.importedRoute.points;
  return state.direction === "outbound" ? state.outbound : state.inbound;
}

function routeDurationFromPoints(points) {
  if (points.length < 2) return 0;
  return Math.max(0, (points.at(-1).time - points[0].time) / 1000);
}

function elapsedRouteSeconds() {
  return routeDurationFromPoints(route());
}

function previewDurationSeconds() {
  return state.previewDurationSeconds[state.direction] || elapsedRouteSeconds();
}

function isPhoneControllable() {
  return state.backend.available && !!state.backend.device;
}

function supportsBackendCapability(capability) {
  return state.backend.capabilities?.[capability] === true;
}

function canControlActiveRoute() {
  return isPhoneControllable() && !!routeId();
}

function isDevicePlaybackActive() {
  const playbackState = state.backend.playback?.state;
  return state.backend.available && (playbackState === "playing" || playbackState === "paused");
}

function playbackDurationSeconds() {
  if (isDevicePlaybackActive() && Number.isFinite(state.backend.playback.durationSeconds)) {
    return state.backend.playback.durationSeconds;
  }
  return previewDurationSeconds();
}

function interpolatedPoint() {
  if (
    state.staticLocation.point
    && !isDevicePlaybackActive()
    && !state.playing
    && state.progress === 0
  ) {
    return state.staticLocation.point;
  }
  return routeProgressPartition().current;
}

function routeProgressPartition() {
  return RouteProgress.partitionTimedRoute(route(), state.progress);
}

function renderShell() {
  const initialCoordinate = interpolatedPoint();
  document.getElementById("app").innerHTML = `
    <section class="app-shell">
      <main class="mission-main">
        <header class="mission-header">
          <div class="product-mark">
            <span class="product-icon" aria-hidden="true"><i></i></span>
            <div><strong>CENTRAL BLUE</strong><span>LOCATION MISSION CONTROL</span></div>
          </div>
          <div class="header-telemetry">
            <section class="header-device-link" data-live-card="device" aria-label="Device link">
              <i class="device-indicator" aria-hidden="true"></i>
              <div class="header-device-name">
                <small>DEVICE LINK</small>
                <strong data-live="device-name">Backend offline</strong>
              </div>
              <div class="header-device-meta">
                <span><small>MODEL</small><b data-live="device-model">—</b></span>
                <span><small>IOS</small><b data-live="device-ios">—</b></span>
              </div>
            </section>
            <span class="status-pill"><i class="status-dot"></i><span data-live="status">Ready to preview</span></span>
          </div>
        </header>

        <div class="mission-content">
          <div class="operations-grid">
            <aside class="route-panel panel">
              <header class="panel-header"><span>ROUTE LIBRARY</span><b data-live="route-total">2</b></header>
              <div class="sidebar-accordions">
                <details class="sidebar-section" data-sidebar-section="home-work" open>
                  <summary><span>HOME AND WORK</span><b>2</b></summary>
                  <div class="route-list">
                    <button class="route-card selected" data-direction="outbound">
                      <span class="route-state"></span>
                      <span class="route-copy"><strong data-route-live="outbound-label">L1 → L2</strong><small data-route-live="outbound-meta">Outbound · 20 min</small></span>
                      <span class="route-count mono" data-route-live="outbound-count">${state.outbound.length}</span>
                    </button>
                    <button class="route-card" data-direction="inbound">
                      <span class="route-state"></span>
                      <span class="route-copy"><strong data-route-live="inbound-label">L2 → L1</strong><small data-route-live="inbound-meta">Return · 20 min</small></span>
                      <span class="route-count mono" data-route-live="inbound-count">${state.inbound.length}</span>
                    </button>
                  </div>
                </details>

                <details class="sidebar-section" data-sidebar-section="route-builder">
                  <summary><span>BUILD ROUTE</span><b>OSRM</b></summary>
                  <div class="sidebar-section-body route-builder">
                    <label class="builder-name">
                      <span>ROUTE NAME</span>
                      <input data-builder-input="name" type="text" maxlength="120" placeholder="Simulated to physical">
                    </label>

                    <section class="builder-link">
                      <label>
                        <span>GOOGLE MAPS DIRECTIONS LINK</span>
                        <input data-builder-input="google-maps-url" type="url" maxlength="2048" placeholder="https://www.google.com/maps/dir/…">
                      </label>
                      <button class="secondary" type="button" data-action="generate-google-maps">LOAD LINK</button>
                      <small>Full coordinate links and maps.app.goo.gl links are supported.</small>
                    </section>

                    <div class="builder-divider"><span>OR SET ENDPOINTS</span></div>

                    <section class="builder-endpoint" data-builder-endpoint="origin">
                      <div class="builder-endpoint-heading">
                        <p>ORIGIN</p>
                        <b data-builder-label="origin">NOT SET</b>
                      </div>
                      <div class="builder-coordinate-grid">
                        <label><span>LAT</span><input class="mono" data-builder-input="origin-latitude" inputmode="decimal" placeholder="37.4158495"></label>
                        <label><span>LON</span><input class="mono" data-builder-input="origin-longitude" inputmode="decimal" placeholder="-122.0349283"></label>
                      </div>
                      <div class="builder-source-row">
                        <button type="button" data-builder-use="simulated" data-builder-target="origin">SIM</button>
                        <button type="button" data-builder-use="physical" data-builder-target="origin">MAC</button>
                        <button type="button" data-builder-pick="origin">MAP</button>
                      </div>
                    </section>

                    <button class="builder-swap" type="button" data-action="swap-builder-endpoints" aria-label="Swap route origin and destination">⇅ SWAP ENDPOINTS</button>

                    <section class="builder-endpoint" data-builder-endpoint="destination">
                      <div class="builder-endpoint-heading">
                        <p>DESTINATION</p>
                        <b data-builder-label="destination">NOT SET</b>
                      </div>
                      <div class="builder-coordinate-grid">
                        <label><span>LAT</span><input class="mono" data-builder-input="destination-latitude" inputmode="decimal" placeholder="37.3920662"></label>
                        <label><span>LON</span><input class="mono" data-builder-input="destination-longitude" inputmode="decimal" placeholder="-122.0947471"></label>
                      </div>
                      <div class="builder-source-row">
                        <button type="button" data-builder-use="simulated" data-builder-target="destination">SIM</button>
                        <button type="button" data-builder-use="physical" data-builder-target="destination">MAC</button>
                        <button type="button" data-builder-pick="destination">MAP</button>
                      </div>
                    </section>

                    <button class="secondary builder-generate" type="button" data-action="generate-directions">GENERATE PREVIEW</button>
                    <div class="builder-feedback" data-live-card="route-builder" aria-live="polite">
                      <strong data-live="builder-status">Choose an origin and destination</strong>
                      <span data-live="builder-detail">SIM uses the current simulated position. MAC uses browser Location Services.</span>
                    </div>
                    <em class="builder-privacy">Generating sends the selected coordinates to the configured OSRM server. They are not logged by Central Blue.</em>
                  </div>
                </details>

                <details class="sidebar-section" data-sidebar-section="import-gpx">
                  <summary><span>IMPORT GPX</span><b data-live="import-total">0</b></summary>
                  <div class="sidebar-section-body">
                    <section class="route-import" aria-labelledby="route-import-title">
                      <p id="route-import-title">ADD A ROUTE FILE</p>
                      <input data-input="gpx-file" type="file" accept=".gpx,application/gpx+xml,application/xml,text/xml" hidden>
                      <button class="secondary import-button" data-action="import-gpx">CHOOSE GPX FILE</button>
                      <div class="import-feedback" data-live-card="import" aria-live="polite">
                        <strong data-live="import-status">No route imported</strong>
                        <span data-live="import-detail">MapsToGPX and standard GPX files are supported.</span>
                      </div>
                      <div class="route-preparation" data-live-card="preparation" hidden>
                        <div class="preparation-heading">
                          <p>IPHONE PLAYBACK</p>
                          <b data-live="preparation-state">NOT PREPARED</b>
                        </div>
                        <label class="timing-option">
                          <input data-input="import-auto-duration" type="checkbox" checked>
                          <span>USE BEST AVAILABLE TRAVEL TIME</span>
                        </label>
                        <div class="preparation-controls">
                          <label>
                            <span>DURATION MIN</span>
                            <input class="mono" data-input="import-duration" type="number" min="0.2" max="1440" step="0.5" value="20">
                          </label>
                          <button class="secondary prepare-button" data-action="prepare-import">PREPARE</button>
                        </div>
                        <em data-live="preparation-note">Choose the playback duration, then build a phone-ready track.</em>
                      </div>
                    </section>
                    <div class="imported-route-list" data-imported-route-list>
                      <p class="empty-route-list">Imported GPX routes will appear here.</p>
                    </div>
                  </div>
                </details>

                <details class="sidebar-section" data-sidebar-section="coordinates">
                  <summary><span>COORDINATES</span><b>LIVE</b></summary>
                  <div class="coordinate-stack">
                    <div class="coordinate-readout">
                      <p>SIMULATED POSITION</p>
                      <div><span>LATITUDE</span><strong class="mono" data-live="latitude">—</strong></div>
                      <div><span>LONGITUDE</span><strong class="mono" data-live="longitude">—</strong></div>
                      <div class="coordinate-copy">
                        <code class="mono" data-live="simulated-coordinate-copy">—</code>
                        <button type="button" data-copy-source="simulated" aria-label="Copy simulated coordinates">COPY</button>
                      </div>
                      <div class="static-location-form" data-live-card="static-location">
                        <p>SET SIMULATED POSITION</p>
                        <label>
                          <span>LATITUDE</span>
                          <input class="mono" data-input="static-latitude" inputmode="decimal" value="${initialCoordinate.lat.toFixed(7)}" aria-label="Simulated latitude">
                        </label>
                        <label>
                          <span>LONGITUDE</span>
                          <input class="mono" data-input="static-longitude" inputmode="decimal" value="${initialCoordinate.lon.toFixed(7)}" aria-label="Simulated longitude">
                        </label>
                        <div class="static-location-actions">
                          <button class="secondary static-map-button" type="button" data-action="pick-static-location" aria-pressed="false">PICK ON MAP</button>
                          <button class="secondary static-activate-button" type="button" data-action="activate-location">ACTIVATE</button>
                        </div>
                        <em data-live="static-location-note">Enter a coordinate, then activate it on the connected iPhone.</em>
                      </div>
                    </div>

                    <div class="coordinate-readout physical-readout">
                      <p>MAC PHYSICAL LOCATION</p>
                      <div><span>LATITUDE</span><strong class="mono" data-live="physical-latitude">—</strong></div>
                      <div><span>LONGITUDE</span><strong class="mono" data-live="physical-longitude">—</strong></div>
                      <div class="coordinate-copy">
                        <code class="mono" data-live="physical-coordinate-copy">Location unavailable</code>
                        <button type="button" data-copy-source="physical" aria-label="Copy Mac physical coordinates" disabled>COPY</button>
                      </div>
                      <button class="secondary location-button" data-action="physical-location"><span data-live="physical-location-label">SHOW MARKER</span></button>
                      <em data-live="physical-location-note">Uses browser Location Services on this Mac, not simulated iPhone GPS.</em>
                    </div>
                  </div>
                </details>
              </div>
            </aside>

            <section class="map-card panel" aria-label="Interactive route map">
              <header class="map-titlebar">
                <span data-live="map-source">CESIUM // WGS84</span>
                <b><span data-live="point-count">${route().length}</span> TRACKED</b>
              </header>
              <div id="cesiumMap"></div>
              <div class="map-actions">
                <button class="map-button" data-action="recenter" aria-label="Recenter route"><span aria-hidden="true">⌖</span> RECENTER</button>
                <button class="map-button" data-action="track-simulated" aria-pressed="false" aria-label="Track simulated location"><span aria-hidden="true">◎</span><span data-live="track-label">TRACK SIM</span></button>
                <button class="map-button" data-action="pan-physical" aria-label="Pan to Mac physical location"><span aria-hidden="true">◆</span> REAL LOCATION</button>
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
                  <label><small>REMAINING MIN</small><input class="mono" data-live-input="remaining-minutes" type="number" min="0.1" step="0.1" value="20"></label>
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
  document.querySelectorAll("[data-builder-use]").forEach(button => {
    button.addEventListener("click", () => {
      useRouteBuilderLocation(
        button.dataset.builderTarget,
        button.dataset.builderUse
      );
    });
  });
  document.querySelectorAll("[data-builder-pick]").forEach(button => {
    button.addEventListener("click", () =>
      setRouteBuilderPickTarget(button.dataset.builderPick)
    );
  });
  document.querySelectorAll(
    '[data-builder-input$="-latitude"], [data-builder-input$="-longitude"]'
  ).forEach(input => {
    input.addEventListener("change", () => {
      try {
        syncBuilderEndpointFromInputs(
          input.dataset.builderInput.startsWith("origin-")
            ? "origin"
            : "destination",
          { manual: true }
        );
      } catch (error) {
        state.routeBuilder.status = "error";
        state.routeBuilder.error = error.message;
        updateRouteBuilderPanel();
      }
    });
  });
  document.querySelector('[data-action="swap-builder-endpoints"]').addEventListener(
    "click",
    swapRouteBuilderEndpoints
  );
  document.querySelector('[data-action="generate-directions"]').addEventListener(
    "click",
    generateDirectionsPreview
  );
  document.querySelector('[data-action="generate-google-maps"]').addEventListener(
    "click",
    generateGoogleMapsPreview
  );
  document.querySelector("[data-imported-route-list]").addEventListener("click", event => {
    const deleteButton = event.target.closest("[data-delete-import]");
    if (deleteButton) {
      deleteImportedRoute(deleteButton.dataset.deleteImport);
      return;
    }
    const button = event.target.closest("[data-imported-filename]");
    if (button) selectImportedRoute(button.dataset.importedFilename);
  });

  document.querySelectorAll("[data-speed]").forEach(button => {
    button.addEventListener("click", () => {
      state.speed = Number(button.dataset.speed);
      updateLiveState();
    });
  });

  document.querySelector('[data-action="toggle"]').addEventListener("click", async () => {
    if (canControlActiveRoute()) {
      await toggleDevicePlayback();
      return;
    }
    togglePreviewPlayback();
  });

  document.querySelector('[data-action="stop"]').addEventListener("click", async () => {
    if (isDevicePlaybackActive()) {
      await stopDevicePlayback();
      return;
    }
    stopPreviewPlayback();
  });

  document.querySelector('[data-action="clear"]').addEventListener("click", clearDeviceLocation);
  document.querySelector('[data-action="activate-location"]').addEventListener(
    "click",
    activateStaticLocation
  );
  document.querySelector('[data-action="pick-static-location"]').addEventListener(
    "click",
    toggleStaticLocationMapPick
  );
  document.querySelectorAll(
    '[data-input="static-latitude"], [data-input="static-longitude"]'
  ).forEach(input => {
    input.addEventListener("change", syncStaticLocationDraftFromInputs);
  });
  document.querySelector('[data-action="recenter"]').addEventListener("click", () => {
    state.trackSimulatedLocation = false;
    frameRoute(true);
    updateLiveState();
  });
  document.querySelector('[data-action="track-simulated"]').addEventListener("click", toggleTrackSimulatedLocation);
  document.querySelector('[data-action="pan-physical"]').addEventListener("click", panToPhysicalLocation);
  document.querySelector('[data-action="physical-location"]').addEventListener("click", togglePhysicalLocationMarker);
  document.querySelectorAll("[data-copy-source]").forEach(button => {
    button.addEventListener("click", () => copyCoordinates(button.dataset.copySource, button));
  });
  const importButton = document.querySelector('[data-action="import-gpx"]');
  const importInput = document.querySelector('[data-input="gpx-file"]');
  importButton.addEventListener("click", () => importInput.click());
  importInput.addEventListener("change", () => importGpxFile(importInput.files[0]));
  document.querySelector('[data-action="prepare-import"]').addEventListener(
    "click",
    prepareActiveImport
  );
  document.querySelector('[data-input="import-auto-duration"]').addEventListener(
    "change",
    updatePreparationPanel
  );
  const remainingInput = document.querySelector('[data-live-input="remaining-minutes"]');
  remainingInput.addEventListener("focus", () => {
    state.previewRemainingInputFocused = true;
  });
  remainingInput.addEventListener("blur", () => {
    state.previewRemainingInputFocused = false;
    syncRemainingInput();
  });
  remainingInput.addEventListener("change", updatePreviewDurationFromRemainingInput);
}

function openSidebarSection(name) {
  const section = document.querySelector(
    `[data-sidebar-section="${name}"]`
  );
  if (!section) return;
  section.open = true;
  section.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function builderInput(name) {
  return document.querySelector(`[data-builder-input="${name}"]`);
}

function setRouteBuilderEndpoint(target, point, label) {
  if (!["origin", "destination"].includes(target)) return;
  state.routeBuilder[target] = {
    lat: Number(point.lat),
    lon: Number(point.lon)
  };
  state.routeBuilder[`${target}Label`] = label;
  const latitudeInput = builderInput(`${target}-latitude`);
  const longitudeInput = builderInput(`${target}-longitude`);
  if (latitudeInput) latitudeInput.value = point.lat.toFixed(7);
  if (longitudeInput) longitudeInput.value = point.lon.toFixed(7);
  state.routeBuilder.error = "";
  state.routeBuilder.status = "idle";
  syncRouteBuilderMarkers();
  frameRouteBuilderEndpoints();
  updateRouteBuilderPanel();
}

function syncBuilderEndpointFromInputs(
  target,
  { required = false, manual = false } = {}
) {
  const latitudeInput = builderInput(`${target}-latitude`);
  const longitudeInput = builderInput(`${target}-longitude`);
  if (!latitudeInput || !longitudeInput) return null;
  const latitudeText = latitudeInput.value.trim();
  const longitudeText = longitudeInput.value.trim();
  if (!latitudeText || !longitudeText) {
    state.routeBuilder[target] = null;
    state.routeBuilder[`${target}Label`] = target === "origin" ? "Origin" : "Destination";
    syncRouteBuilderMarkers();
    if (required) {
      throw new Error(`Enter both ${target} latitude and longitude.`);
    }
    updateRouteBuilderPanel();
    return null;
  }

  const lat = Number(latitudeText);
  const lon = Number(longitudeText);
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
    throw new Error(`${target === "origin" ? "Origin" : "Destination"} latitude must be between -90 and 90.`);
  }
  if (!Number.isFinite(lon) || lon < -180 || lon > 180) {
    throw new Error(`${target === "origin" ? "Origin" : "Destination"} longitude must be between -180 and 180.`);
  }
  state.routeBuilder[target] = { lat, lon };
  if (
    manual
    || !state.routeBuilder[`${target}Label`]
    || ["Origin", "Destination"].includes(state.routeBuilder[`${target}Label`])
  ) {
    state.routeBuilder[`${target}Label`] = target === "origin" ? "Manual origin" : "Manual destination";
  }
  syncRouteBuilderMarkers();
  frameRouteBuilderEndpoints();
  updateRouteBuilderPanel();
  return state.routeBuilder[target];
}

function useRouteBuilderLocation(target, source) {
  if (isDevicePlaybackActive()) {
    state.routeBuilder.status = "error";
    state.routeBuilder.error = "Stop phone playback before changing route endpoints.";
    updateRouteBuilderPanel();
    return;
  }
  if (source === "simulated") {
    const current = interpolatedPoint();
    setRouteBuilderEndpoint(target, current, "Simulated");
    return;
  }
  if (source !== "physical") return;
  if (state.physicalLocation.coords) {
    setRouteBuilderEndpoint(
      target,
      state.physicalLocation.coords,
      "Mac physical"
    );
    return;
  }

  state.physicalLocation.pendingBuilderTarget = target;
  state.routeBuilder.status = "requesting-location";
  state.routeBuilder.error = "";
  if (state.physicalLocation.watchId === null) {
    startPhysicalLocationWatch();
  } else {
    updateRouteBuilderPanel();
  }
}

function setRouteBuilderPickTarget(target) {
  if (!["origin", "destination"].includes(target)) return;
  if (isDevicePlaybackActive()) {
    state.routeBuilder.status = "error";
    state.routeBuilder.error = "Stop phone playback before changing route endpoints.";
    updateRouteBuilderPanel();
    return;
  }
  const nextTarget = state.routeBuilder.pickTarget === target ? null : target;
  if (nextTarget) {
    state.staticLocation.pickMode = false;
    updateStaticLocationPanel();
  }
  state.routeBuilder.pickTarget = nextTarget;
  state.routeBuilder.status = nextTarget ? "picking" : "idle";
  state.routeBuilder.error = "";
  if (state.viewer && !state.viewer.isDestroyed()) {
    state.viewer.scene.canvas.style.cursor = nextTarget ? "crosshair" : "";
  }
  updateRouteBuilderPanel();
}

function swapRouteBuilderEndpoints() {
  const origin = state.routeBuilder.origin;
  const originLabel = state.routeBuilder.originLabel;
  state.routeBuilder.origin = state.routeBuilder.destination;
  state.routeBuilder.originLabel = state.routeBuilder.destinationLabel;
  state.routeBuilder.destination = origin;
  state.routeBuilder.destinationLabel = originLabel;

  ["origin", "destination"].forEach(target => {
    const point = state.routeBuilder[target];
    const latitudeInput = builderInput(`${target}-latitude`);
    const longitudeInput = builderInput(`${target}-longitude`);
    if (latitudeInput) latitudeInput.value = point ? point.lat.toFixed(7) : "";
    if (longitudeInput) longitudeInput.value = point ? point.lon.toFixed(7) : "";
  });
  state.routeBuilder.status = "idle";
  state.routeBuilder.error = "";
  syncRouteBuilderMarkers();
  frameRouteBuilderEndpoints();
  updateRouteBuilderPanel();
}

function frameRouteBuilderEndpoints() {
  if (!state.routeBuilder.origin || !state.routeBuilder.destination) return;
  frameRoute(
    true,
    [state.routeBuilder.origin, state.routeBuilder.destination]
  );
}

async function generateDirectionsPreview() {
  if (isDevicePlaybackActive()) {
    state.routeBuilder.status = "error";
    state.routeBuilder.error = "Stop phone playback before generating another route.";
    updateRouteBuilderPanel();
    return;
  }
  if (!state.backend.available || !supportsBackendCapability("directionsGeneration")) {
    state.routeBuilder.status = "error";
    state.routeBuilder.error = "Restart the local controller to enable route generation.";
    updateRouteBuilderPanel();
    return;
  }

  let origin;
  let destination;
  try {
    origin = syncBuilderEndpointFromInputs("origin", { required: true });
    destination = syncBuilderEndpointFromInputs("destination", { required: true });
  } catch (error) {
    state.routeBuilder.status = "error";
    state.routeBuilder.error = error.message;
    updateRouteBuilderPanel();
    return;
  }

  state.routeBuilder.status = "generating";
  state.routeBuilder.error = "";
  state.routeBuilder.pickTarget = null;
  if (state.viewer && !state.viewer.isDestroyed()) {
    state.viewer.scene.canvas.style.cursor = "";
  }
  updateRouteBuilderPanel();
  try {
    const nameInput = builderInput("name");
    const response = await apiRequest("/api/routes/from-directions", {
      method: "POST",
      body: JSON.stringify({
        name: nameInput?.value.trim() || undefined,
        origin: {
          latitude: origin.lat,
          longitude: origin.lon
        },
        destination: {
          latitude: destination.lat,
          longitude: destination.lon
        },
        originLabel: state.routeBuilder.originLabel,
        destinationLabel: state.routeBuilder.destinationLabel,
        profile: "driving"
      })
    });
    acceptGeneratedRoutePreview(response);
  } catch (error) {
    state.routeBuilder.status = "error";
    state.routeBuilder.error = error.message || "The route preview could not be generated.";
    updateRouteBuilderPanel();
  }
}

async function generateGoogleMapsPreview() {
  if (isDevicePlaybackActive()) {
    state.routeBuilder.status = "error";
    state.routeBuilder.error = "Stop phone playback before generating another route.";
    updateRouteBuilderPanel();
    return;
  }
  if (!state.backend.available || !supportsBackendCapability("googleMapsLinks")) {
    state.routeBuilder.status = "error";
    state.routeBuilder.error = "Restart the local controller to enable Google Maps links.";
    updateRouteBuilderPanel();
    return;
  }
  const linkInput = builderInput("google-maps-url");
  const url = linkInput?.value.trim() || "";
  if (!url) {
    state.routeBuilder.status = "error";
    state.routeBuilder.error = "Paste a Google Maps directions link first.";
    updateRouteBuilderPanel();
    return;
  }

  state.routeBuilder.status = "resolving-link";
  state.routeBuilder.error = "";
  state.routeBuilder.pickTarget = null;
  updateRouteBuilderPanel();
  try {
    const nameInput = builderInput("name");
    const response = await apiRequest("/api/routes/from-google-maps-link", {
      method: "POST",
      body: JSON.stringify({
        url,
        name: nameInput?.value.trim() || undefined
      })
    });
    acceptGeneratedRoutePreview(response);
  } catch (error) {
    state.routeBuilder.status = "error";
    state.routeBuilder.error = error.code === "google_maps_geocoding_required"
      ? `${error.message} Use a coordinate-based directions link or enter the endpoints below.`
      : error.message || "The Google Maps directions link could not be loaded.";
    updateRouteBuilderPanel();
  }
}

function acceptGeneratedRoutePreview(response) {
  const points = playbackPointsFromPayload(response.previewPoints);
  if (points.length < 2) {
    throw new Error("The routing provider returned an empty preview.");
  }
  const { previewPoints, ...metadata } = response;
  const item = { metadata, points };
  state.importedRoute.items = [
    item,
    ...state.importedRoute.items.filter(
      candidate => candidate.metadata.filename !== metadata.filename
    )
  ];
  const requestedOrigin = metadata.requestedOrigin;
  const requestedDestination = metadata.requestedDestination;
  if (requestedOrigin) {
    setRouteBuilderEndpoint(
      "origin",
      { lat: requestedOrigin.latitude, lon: requestedOrigin.longitude },
      metadata.originLabel || "Generated origin"
    );
  }
  if (requestedDestination) {
    setRouteBuilderEndpoint(
      "destination",
      {
        lat: requestedDestination.latitude,
        lon: requestedDestination.longitude
      },
      metadata.destinationLabel || "Generated destination"
    );
  }
  state.routeBuilder.status = "ready";
  state.routeBuilder.error = "";
  state.routeBuilder.lastMetadata = metadata;
  activateImportedRoute(item);
  openSidebarSection("import-gpx");
  updateRouteBuilderPanel();
}

function updateRouteBuilderPanel() {
  const card = document.querySelector('[data-live-card="route-builder"]');
  const generateButton = document.querySelector('[data-action="generate-directions"]');
  const linkButton = document.querySelector('[data-action="generate-google-maps"]');
  const linkInput = builderInput("google-maps-url");
  if (!card || !generateButton || !linkButton) return;
  const status = state.routeBuilder.status;
  const metadata = state.routeBuilder.lastMetadata;
  const capabilityAvailable = supportsBackendCapability("directionsGeneration");
  const linkCapabilityAvailable = supportsBackendCapability("googleMapsLinks");
  const busy = status === "generating"
    || status === "resolving-link"
    || status === "requesting-location";

  card.classList.toggle("ready", status === "ready");
  card.classList.toggle("error", status === "error");
  card.classList.toggle("loading", busy || status === "picking");
  generateButton.disabled = busy
    || isDevicePlaybackActive()
    || !state.backend.available
    || !capabilityAvailable;
  linkButton.disabled = busy
    || isDevicePlaybackActive()
    || !state.backend.available
    || !linkCapabilityAvailable;
  if (linkInput) linkInput.disabled = busy || isDevicePlaybackActive();
  document.querySelectorAll(
    "[data-builder-use], [data-builder-pick], [data-action='swap-builder-endpoints']"
  ).forEach(button => {
    button.disabled = busy || isDevicePlaybackActive();
  });
  generateButton.textContent = status === "generating"
    ? "GENERATING…"
    : "GENERATE PREVIEW";
  linkButton.textContent = status === "resolving-link"
    ? "LOADING…"
    : "LOAD LINK";

  document.querySelectorAll("[data-builder-pick]").forEach(button => {
    const selected = state.routeBuilder.pickTarget === button.dataset.builderPick;
    button.classList.toggle("selected", selected);
    button.textContent = selected ? "PICKING" : "MAP";
  });
  ["origin", "destination"].forEach(target => {
    const endpoint = document.querySelector(`[data-builder-endpoint="${target}"]`);
    const label = document.querySelector(`[data-builder-label="${target}"]`);
    endpoint?.classList.toggle("set", !!state.routeBuilder[target]);
    if (label) {
      label.textContent = state.routeBuilder[target]
        ? state.routeBuilder[`${target}Label`]
        : "NOT SET";
    }
  });

  if (status === "error") {
    setText("builder-status", "Route generation needs attention");
    setText("builder-detail", state.routeBuilder.error);
  } else if (status === "generating") {
    setText("builder-status", "Requesting road directions");
    setText("builder-detail", "OSRM is building full driving geometry and travel timing.");
  } else if (status === "resolving-link") {
    setText("builder-status", "Resolving Google Maps directions");
    setText("builder-detail", "Checking the link, extracting coordinate endpoints, and requesting OSRM road geometry.");
  } else if (status === "requesting-location") {
    setText("builder-status", "Waiting for Mac location");
    setText("builder-detail", "Approve browser Location Services if macOS asks.");
  } else if (status === "picking") {
    setText("builder-status", `Pick ${state.routeBuilder.pickTarget} on the map`);
    setText("builder-detail", "Click once on the Cesium map to capture the coordinate.");
  } else if (status === "ready" && metadata) {
    setText("builder-status", `${metadata.name || "Generated route"} preview ready`);
    setText(
      "builder-detail",
      `${metadata.provider || "Routing provider"} · ${formatDistance(metadata.distanceMeters)} · ETA ${durationMinutes(metadata.estimatedDurationSeconds)} · prepare it for phone playback`
    );
  } else if (!state.backend.available) {
    setText("builder-status", "Local backend required");
    setText("builder-detail", "Start the route controller before generating road directions.");
  } else if (!capabilityAvailable) {
    setText("builder-status", "Controller restart required");
    setText("builder-detail", "Restart the local controller to load Phase 3F route generation.");
  } else {
    setText("builder-status", "Choose an origin and destination");
    setText("builder-detail", "SIM uses the current simulated position. MAC uses browser Location Services.");
  }
}

function formatDistance(meters) {
  if (!Number.isFinite(meters) || meters <= 0) return "—";
  const miles = meters / 1609.344;
  return miles >= 10 ? `${miles.toFixed(0)} mi` : `${miles.toFixed(1)} mi`;
}

function togglePreviewPlayback() {
  if (state.progress >= 1) state.progress = 0;
  state.playing = !state.playing;
  state.lastFrame = null;
  updateLiveState();
}

function stopPreviewPlayback() {
  state.playing = false;
  state.progress = 0;
  state.lastFrame = null;
  updateLiveState();
}

function updatePreviewDurationFromRemainingInput(event) {
  const remainingMinutes = Number(event.target.value);
  if (!Number.isFinite(remainingMinutes) || remainingMinutes <= 0) {
    syncRemainingInput();
    return;
  }
  const currentTotal = playbackDurationSeconds();
  const elapsed = currentTotal * state.progress;
  state.previewDurationSeconds[state.direction] = Math.max(1, elapsed + remainingMinutes * 60);
  state.lastFrame = null;
  updateLiveState();
}

function setDirection(direction) {
  if (direction === "imported" && !state.importedRoute.points.length) return;
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
  await refreshBackendRoutes();
  await refreshImportedRoutes();
  window.setInterval(refreshBackendStatus, 1000);
}

async function apiRequest(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.code = payload.errorCode || "";
    error.detail = payload.errorDetail || "";
    throw error;
  }
  return payload;
}

async function refreshBackendStatus() {
  try {
    const payload = await apiRequest("/api/status");
    state.backend.available = true;
    state.backend.apiVersion = Number(payload.apiVersion) || 1;
    state.backend.capabilities = payload.capabilities || {};
    state.backend.deviceReport = payload.device || null;
    state.backend.device = parseDevice(payload.device);
    state.backend.playback = payload.playback || { state: "idle" };
    const simulatedLocation = payload.simulatedLocation;
    if (
      simulatedLocation
      && Number.isFinite(simulatedLocation.latitude)
      && Number.isFinite(simulatedLocation.longitude)
      && state.staticLocation.status !== "activating"
    ) {
      state.staticLocation.status = "active";
      state.staticLocation.point = {
        lat: simulatedLocation.latitude,
        lon: simulatedLocation.longitude
      };
      state.staticLocation.error = "";
    } else if (
      !simulatedLocation
      && state.staticLocation.status === "active"
    ) {
      state.staticLocation.status = "idle";
      state.staticLocation.point = null;
    }
    state.backend.error = state.backend.playback.error?.message || "";
    syncBackendPlayback();
  } catch (error) {
    state.backend.available = false;
    state.backend.apiVersion = 0;
    state.backend.capabilities = {};
    state.backend.device = null;
    state.backend.deviceReport = null;
    state.backend.error = "";
    state.backend.playback = { state: "idle" };
    if (state.staticLocation.status === "active") {
      state.staticLocation.status = "idle";
      state.staticLocation.point = null;
    }
  }
  updateLiveState();
}

async function refreshBackendRoutes() {
  if (!state.backend.available) {
    updateRouteCards();
    return;
  }
  try {
    const payload = await apiRequest("/api/routes");
    state.backend.routes = Array.isArray(payload.routes) ? payload.routes : [];
  } catch (error) {
    state.backend.routes = [];
    state.backend.error = error.message;
  }
  updateRouteCards();
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

function deviceProbeMessage(report) {
  if (!report || !report.device_probe_attempted) return "Run ./scripts/run_frontend.sh for live phone control.";
  if (report.device_probe_ok) return "";
  const output = report.device_probe_output || "";
  if (/no usb-connected iphone/i.test(output)) {
    return "No USB iPhone detected. Plug in the phone, unlock it, and tap Trust if prompted.";
  }
  if (/unable to connect to tunneld/i.test(output)) {
    return "Developer tunnel is not running. Start tunneld, or reconnect and retry with userspace mode.";
  }
  if (/developer mode/i.test(output)) {
    return "Enable Developer Mode on the iPhone, then reconnect it.";
  }
  return output || "Device probe failed. Reconnect and unlock the iPhone, then retry.";
}

function preparedRouteForImport(metadata) {
  if (!metadata?.storedPath) return null;
  return state.backend.routes.find(candidate =>
    candidate.direction === "custom"
    && candidate.sourcePath === metadata.storedPath
  ) || null;
}

function backendRouteForDirection(direction) {
  return state.backend.routes.find(candidate => candidate.direction === direction);
}

function fallbackRouteMeta(direction) {
  const points = direction === "outbound" ? state.outbound : state.inbound;
  return {
    id: direction === "outbound" ? "l1-to-l2" : "l2-to-l1",
    label: direction === "outbound" ? "L1 to L2" : "L2 to L1",
    direction,
    pointCount: points.length,
    durationSeconds: points.length > 1 ? Math.max(0, (points.at(-1).time - points[0].time) / 1000) : 0
  };
}

function displayRouteLabel(label) {
  return label.replace(/\s+to\s+/i, " → ");
}

function updateRouteCards() {
  ["outbound", "inbound"].forEach(direction => {
    const routeMeta = backendRouteForDirection(direction) || fallbackRouteMeta(direction);
    const prefix = direction === "outbound" ? "Outbound" : "Return";
    setRouteText(`${direction}-label`, displayRouteLabel(routeMeta.label));
    setRouteText(`${direction}-meta`, `${prefix} · ${durationMinutes(routeMeta.durationSeconds)}`);
    setRouteText(`${direction}-count`, String(routeMeta.pointCount));
  });

  const imported = state.importedRoute;
  setText("route-total", String(2 + imported.items.length));
  setText("import-total", String(imported.items.length));
  renderImportedRouteList();
  updateImportPanel();
}

function renderImportedRouteList() {
  const container = document.querySelector("[data-imported-route-list]");
  if (!container) return;
  const items = state.importedRoute.items;
  if (!items.length) {
    container.innerHTML = '<p class="empty-route-list">Imported GPX routes will appear here.</p>';
    return;
  }

  const activeFilename = state.direction === "imported"
    ? state.importedRoute.metadata?.filename
    : "";
  container.innerHTML = items.map(item => {
    const metadata = item.metadata;
    const selected = metadata.filename === activeFilename;
    const prepared = preparedRouteForImport(metadata);
    const deleting = state.importedRoute.deletingFilename === metadata.filename;
    const deletionAvailable = supportsBackendCapability("deleteImports");
    const timing = prepared
      ? `Phone ready · ${timingModeLabel(prepared)} · ${durationMinutes(prepared.durationSeconds)}`
      : metadata.hasTimestamps
        ? "Timed · prepare for phone"
        : "Untimed · prepare for phone";
    const routeLabel = metadata.name
      || metadata.originalFilename
      || metadata.filename;
    const sourceLabel = metadata.sourceType === "google-maps"
      ? "Google Maps"
      : metadata.sourceType === "directions"
        ? "Generated"
        : "Imported";
    return `
      <div class="imported-route-row">
        <button class="route-card imported-route-card${selected ? " selected" : ""}" data-imported-filename="${metadata.filename}"${deleting ? " disabled" : ""}>
          <span class="route-state"></span>
          <span class="route-copy">
            <strong>${escapeHtml(routeLabel)}</strong>
            <small>${sourceLabel} · ${timing}</small>
          </span>
          <span class="route-count mono">${metadata.pointCount}</span>
        </button>
        <button class="import-delete-button" data-delete-import="${metadata.filename}" aria-label="Delete ${escapeHtml(routeLabel)}"${deleting || isDevicePlaybackActive() || !deletionAvailable ? " disabled" : ""} title="${deletionAvailable ? "Delete imported GPX" : "Restart the controller to enable deletion"}">${deleting ? "…" : "DELETE"}</button>
      </div>`;
  }).join("");
}

function updateImportPanel() {
  const imported = state.importedRoute;
  const button = document.querySelector('[data-action="import-gpx"]');
  const feedback = document.querySelector('[data-live-card="import"]');
  if (!button || !feedback) return;

  button.disabled = imported.status === "uploading"
    || imported.status === "loading"
    || imported.status === "deleting"
    || isDevicePlaybackActive();
  button.textContent = imported.status === "uploading"
    ? "IMPORTING…"
    : imported.status === "loading"
      ? "OPENING ROUTE…"
    : imported.status === "deleting"
      ? "DELETING ROUTE…"
    : imported.status === "ready"
      ? "IMPORT ANOTHER GPX"
      : "CHOOSE GPX FILE";
  feedback.classList.toggle("ready", imported.status === "ready");
  feedback.classList.toggle(
    "error",
    imported.status === "error" || imported.status === "delete-error"
  );
  feedback.classList.toggle(
    "loading",
    imported.status === "uploading"
      || imported.status === "loading"
      || imported.status === "deleting"
  );

  if (imported.status === "uploading") {
    setText("import-status", "Validating route");
    setText("import-detail", "Checking GPX geometry and saving it to this Mac…");
  } else if (imported.status === "loading") {
    setText("import-status", "Opening saved route");
    setText("import-detail", "Loading its coordinates into the Cesium preview…");
  } else if (imported.status === "deleting") {
    setText("import-status", "Deleting imported route");
    setText("import-detail", "Removing the saved GPX and its prepared playback track…");
  } else if (imported.status === "ready") {
    const metadata = imported.metadata;
    const prepared = preparedRouteForImport(metadata);
    setText("import-status", metadata.name || metadata.originalFilename || "Imported route");
    setText(
      "import-detail",
      `${metadata.pointCount.toLocaleString()} points · ${["directions", "google-maps"].includes(metadata.sourceType) ? `${metadata.provider} ${formatDistance(metadata.distanceMeters)} ETA ${durationMinutes(metadata.estimatedDurationSeconds)}` : prepared ? `${timingModeLabel(prepared)} ${durationMinutes(prepared.durationSeconds)}` : metadata.hasTimestamps ? "timestamps available" : "road timing available when prepared"} · saved as ${metadata.filename}`
    );
  } else if (imported.status === "error") {
    setText("import-status", "Import failed");
    setText("import-detail", imported.error);
  } else if (imported.status === "delete-error") {
    setText("import-status", "Delete failed");
    setText("import-detail", imported.error);
  } else if (!state.backend.available) {
    setText("import-status", "Local backend required");
    setText("import-detail", "Start the route controller, then choose a MapsToGPX or standard GPX file.");
  } else {
    setText(
      "import-status",
      imported.items.length
        ? `${imported.items.length} imported ${imported.items.length === 1 ? "route" : "routes"}`
        : "Ready for a GPX file"
    );
    setText(
      "import-detail",
      imported.items.length
        ? "Choose a saved route below or add another GPX file."
        : "MapsToGPX and standard GPX files are supported."
    );
  }
  updatePreparationPanel();
}

function updatePreparationPanel() {
  const panel = document.querySelector('[data-live-card="preparation"]');
  const input = document.querySelector('[data-input="import-duration"]');
  const autoDurationInput = document.querySelector('[data-input="import-auto-duration"]');
  const button = document.querySelector('[data-action="prepare-import"]');
  const metadata = state.importedRoute.metadata;
  if (!panel || !input || !autoDurationInput || !button) return;

  const activeImport = state.direction === "imported" && !!metadata;
  panel.hidden = !activeImport;
  if (!activeImport) return;

  const prepared = preparedRouteForImport(metadata);
  const routeAwareSupported = supportsBackendCapability("routeAwareTiming");
  if (input.dataset.filename !== metadata.filename) {
    input.dataset.filename = metadata.filename;
    input.value = formatMinutesInput(
      (prepared?.durationSeconds || state.previewDurationSeconds.imported || 1200)
      / 60
    );
    autoDurationInput.checked = !prepared
      || (
        Number.isFinite(prepared.estimatedDurationSeconds)
        && Math.abs(
          prepared.durationSeconds - prepared.estimatedDurationSeconds
        ) < 1
      );
  }
  if (!routeAwareSupported && autoDurationInput.checked) {
    autoDurationInput.checked = false;
  }

  const preparationStatus = state.importedRoute.preparationStatus;
  const preparing = preparationStatus === "preparing";
  const preparationError = state.importedRoute.preparationError;
  panel.classList.toggle("ready", !!prepared && !preparationError);
  panel.classList.toggle("error", !!preparationError);
  panel.classList.toggle("loading", preparing);
  panel.classList.toggle(
    "fallback",
    !!prepared && prepared.timingMode === "uniform" && !!prepared.timingWarning
  );
  autoDurationInput.disabled = preparing
    || isDevicePlaybackActive()
    || !routeAwareSupported;
  input.disabled = autoDurationInput.checked
    || preparing
    || isDevicePlaybackActive();
  button.disabled = preparing || isDevicePlaybackActive();
  button.textContent = preparing
    ? "BUILDING…"
    : prepared
      ? "REBUILD TRACK"
      : "PREPARE";
  setText(
    "preparation-state",
    preparing
      ? "BUILDING"
      : preparationError
        ? "ERROR"
        : prepared
          ? timingModeLabel(prepared).toUpperCase()
          : "NOT PREPARED"
  );
  setText(
    "preparation-note",
    preparationError
      || (!routeAwareSupported
        ? "Restart the local controller to enable route-aware timing and GPX deletion."
        : prepared
        ? timingPreparationSummary(prepared)
        : metadata.hasTimestamps
          ? "Best available preserves the GPX speed profile. Uncheck it to scale the route to a custom duration."
          : "Best available sends up to 90 sampled coordinates to OSRM for road timing. Uncheck it to use a custom duration.")
  );
}

function timingModeLabel(routeMeta) {
  if (routeMeta?.timingMode === "route-aware") {
    return routeMeta.timingProvider
      ? `${routeMeta.timingProvider} road timing`
      : "Road timing";
  }
  if (routeMeta?.timingMode === "source") return "Source timing";
  if (routeMeta?.timingMode === "uniform") return "Uniform timing";
  return "Prepared timing";
}

function timingPreparationSummary(routeMeta) {
  const summary = [
    timingModeLabel(routeMeta),
    durationMinutes(routeMeta.durationSeconds),
    `${routeMeta.pointCount.toLocaleString()} half-second points`
  ];
  if (
    routeMeta.timingMode === "route-aware"
    && Number.isFinite(routeMeta.estimatedDurationSeconds)
    && Math.abs(routeMeta.durationSeconds - routeMeta.estimatedDurationSeconds) >= 1
  ) {
    summary.push(`road ETA ${durationMinutes(routeMeta.estimatedDurationSeconds)}`);
  }
  if (routeMeta.timingWarning) summary.push(routeMeta.timingWarning);
  return summary.join(" · ");
}

function durationMinutes(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  const minutes = Math.round(seconds / 60);
  return `${minutes} min`;
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
    state.staticLocation.status = "idle";
    state.staticLocation.point = null;
    state.staticLocation.draftPoint = null;
    state.staticLocation.pickMode = false;
    state.staticLocation.error = "";
    if (state.viewer && !state.viewer.isDestroyed()) {
      state.viewer.scene.canvas.style.cursor = "";
      syncStaticLocationTargetMarker();
    }
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
      state.staticLocation.status = "idle";
      state.staticLocation.point = null;
      state.staticLocation.draftPoint = null;
      state.staticLocation.pickMode = false;
      state.staticLocation.error = "";
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
    state.staticLocation.status = "idle";
    state.staticLocation.point = null;
    state.staticLocation.draftPoint = null;
    state.staticLocation.pickMode = false;
    state.staticLocation.error = "";
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
    state.staticLocation.status = "idle";
    state.staticLocation.point = null;
    state.staticLocation.draftPoint = null;
    state.staticLocation.pickMode = false;
    state.staticLocation.error = "";
    if (state.viewer && !state.viewer.isDestroyed()) {
      state.viewer.scene.canvas.style.cursor = "";
      syncStaticLocationTargetMarker();
    }
  } catch (error) {
    state.backend.error = error.message;
  }
  updateLiveState();
}

function toggleStaticLocationMapPick() {
  if (isDevicePlaybackActive()) {
    state.staticLocation.status = "error";
    state.staticLocation.error = "Stop route playback before choosing a position.";
    updateStaticLocationPanel();
    return;
  }
  if (!state.viewer || state.viewer.isDestroyed()) {
    state.staticLocation.status = "error";
    state.staticLocation.error = "The map is not available for coordinate selection.";
    updateStaticLocationPanel();
    return;
  }

  state.staticLocation.pickMode = !state.staticLocation.pickMode;
  state.staticLocation.error = "";
  if (state.staticLocation.pickMode) {
    state.routeBuilder.pickTarget = null;
    if (state.routeBuilder.status === "picking") {
      state.routeBuilder.status = "idle";
    }
  }
  state.viewer.scene.canvas.style.cursor = state.staticLocation.pickMode
    ? "crosshair"
    : "";
  updateRouteBuilderPanel();
  updateStaticLocationPanel();
}

function syncStaticLocationDraftFromInputs() {
  const latitudeInput = document.querySelector(
    '[data-input="static-latitude"]'
  );
  const longitudeInput = document.querySelector(
    '[data-input="static-longitude"]'
  );
  if (!latitudeInput || !longitudeInput) return null;
  const latitude = Number(latitudeInput.value.trim());
  const longitude = Number(longitudeInput.value.trim());
  if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) {
    state.staticLocation.status = "error";
    state.staticLocation.error = "Latitude must be between -90 and 90.";
    state.staticLocation.draftPoint = null;
    syncStaticLocationTargetMarker();
    updateStaticLocationPanel();
    return null;
  }
  if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
    state.staticLocation.status = "error";
    state.staticLocation.error = "Longitude must be between -180 and 180.";
    state.staticLocation.draftPoint = null;
    syncStaticLocationTargetMarker();
    updateStaticLocationPanel();
    return null;
  }

  state.staticLocation.draftPoint = { lat: latitude, lon: longitude };
  state.staticLocation.error = "";
  if (state.staticLocation.status === "error") {
    state.staticLocation.status = state.staticLocation.point
      ? "active"
      : "idle";
  }
  syncStaticLocationTargetMarker();
  updateStaticLocationPanel();
  return state.staticLocation.draftPoint;
}

async function activateStaticLocation() {
  const latitudeInput = document.querySelector(
    '[data-input="static-latitude"]'
  );
  const longitudeInput = document.querySelector(
    '[data-input="static-longitude"]'
  );
  if (!latitudeInput || !longitudeInput) return;
  if (isDevicePlaybackActive()) {
    state.staticLocation.status = "error";
    state.staticLocation.error = "Stop route playback before activating a position.";
    updateStaticLocationPanel();
    return;
  }
  if (
    !state.backend.available
    || !supportsBackendCapability("staticLocation")
  ) {
    state.staticLocation.status = "error";
    state.staticLocation.error = "Restart the local controller to enable static positions.";
    updateStaticLocationPanel();
    return;
  }

  const point = syncStaticLocationDraftFromInputs();
  if (!point) return;
  const { lat: latitude, lon: longitude } = point;

  state.staticLocation.status = "activating";
  state.staticLocation.pickMode = false;
  state.staticLocation.error = "";
  if (state.viewer && !state.viewer.isDestroyed()) {
    state.viewer.scene.canvas.style.cursor = "";
  }
  updateStaticLocationPanel();
  try {
    const activated = await apiRequest("/api/location/set", {
      method: "POST",
      body: JSON.stringify({ latitude, longitude })
    });
    state.staticLocation.status = "active";
    state.staticLocation.point = {
      lat: activated.latitude,
      lon: activated.longitude
    };
    state.staticLocation.draftPoint = null;
    state.staticLocation.error = "";
    state.backend.error = "";
    state.playing = false;
    state.progress = 0;
    state.lastFrame = null;
    syncStaticLocationTargetMarker();
    updateLiveState();
    panToPoint(state.staticLocation.point, true);
  } catch (error) {
    state.staticLocation.status = "error";
    state.staticLocation.error = error.message
      || "The simulated position could not be activated.";
    updateStaticLocationPanel();
  }
}

function updateStaticLocationPanel() {
  const card = document.querySelector(
    '[data-live-card="static-location"]'
  );
  const button = document.querySelector(
    '[data-action="activate-location"]'
  );
  const mapButton = document.querySelector(
    '[data-action="pick-static-location"]'
  );
  const latitudeInput = document.querySelector(
    '[data-input="static-latitude"]'
  );
  const longitudeInput = document.querySelector(
    '[data-input="static-longitude"]'
  );
  if (
    !card
    || !button
    || !mapButton
    || !latitudeInput
    || !longitudeInput
  ) return;

  const status = state.staticLocation.status;
  const activating = status === "activating";
  const capable = supportsBackendCapability("staticLocation");
  const disabled = activating
    || isDevicePlaybackActive()
    || !state.backend.available
    || !capable
    || !isPhoneControllable();
  card.classList.toggle("active", status === "active");
  card.classList.toggle("error", status === "error");
  card.classList.toggle(
    "loading",
    activating || state.staticLocation.pickMode
  );
  button.disabled = disabled;
  mapButton.disabled = activating || isDevicePlaybackActive();
  latitudeInput.disabled = activating || isDevicePlaybackActive();
  longitudeInput.disabled = activating || isDevicePlaybackActive();
  button.textContent = activating ? "ACTIVATING…" : "ACTIVATE";
  mapButton.textContent = state.staticLocation.pickMode
    ? "PICKING…"
    : "PICK ON MAP";
  mapButton.setAttribute(
    "aria-pressed",
    state.staticLocation.pickMode ? "true" : "false"
  );

  let note = "Enter a coordinate or pick one on the map, then press Activate.";
  if (state.staticLocation.error) {
    note = state.staticLocation.error;
  } else if (state.staticLocation.pickMode) {
    note = "Click one point on the Cesium map to fill the coordinate fields.";
  } else if (state.staticLocation.draftPoint) {
    note = `${coordinateText(state.staticLocation.draftPoint)} selected · press Activate`;
  } else if (status === "active" && state.staticLocation.point) {
    note = `Active at ${coordinateText(state.staticLocation.point)}`;
  } else if (!state.backend.available) {
    note = "Start the local controller before activating a position.";
  } else if (!capable) {
    note = "Restart the local controller to enable static positions.";
  } else if (!isPhoneControllable()) {
    note = "Connect and unlock the iPhone before activating a position.";
  }
  setText("static-location-note", note);
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
    state.viewer.scene.screenSpaceCameraController.minimumZoomDistance = 5;
    state.viewer.scene.screenSpaceCameraController.maximumZoomDistance = 25000000;
    syncMapRoute();
    initRouteBuilderMapPicking();
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
  const partition = routeProgressPartition();
  state.mapRouteGeometry.completed = degreesArray(partition.completed, 70);
  state.mapRouteGeometry.remaining = degreesArray(partition.remaining, 70);
  const start = points[0];
  const end = points.at(-1);

  state.mapEntities.route = viewer.entities.add({
    polyline: {
      positions: new Cesium.CallbackProperty(
        () => state.mapRouteGeometry.remaining,
        false
      ),
      width: 6,
      material: Cesium.Color.fromCssColorString("#43a2d8"),
      depthFailMaterial: Cesium.Color.fromCssColorString("#43a2d8"),
      clampToGround: false
    }
  });
  state.mapEntities.progress = viewer.entities.add({
    polyline: {
      positions: new Cesium.CallbackProperty(
        () => state.mapRouteGeometry.completed,
        false
      ),
      width: 6,
      material: Cesium.Color.fromCssColorString("#8cffb2"),
      depthFailMaterial: Cesium.Color.fromCssColorString("#8cffb2"),
      clampToGround: false
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
  syncRouteBuilderMarkers();
  syncStaticLocationTargetMarker();
  viewer.scene.requestRender();
}

function initRouteBuilderMapPicking() {
  const viewer = state.viewer;
  if (!viewer || viewer.isDestroyed()) return;
  if (state.mapPickHandler && !state.mapPickHandler.isDestroyed()) {
    state.mapPickHandler.destroy();
  }
  state.mapPickHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  state.mapPickHandler.setInputAction(movement => {
    const target = state.routeBuilder.pickTarget;
    const staticPick = state.staticLocation.pickMode;
    if (!target && !staticPick) return;
    const cartesian = viewer.camera.pickEllipsoid(
      movement.position,
      viewer.scene.globe.ellipsoid
    );
    if (!cartesian) {
      if (staticPick) {
        state.staticLocation.status = "error";
        state.staticLocation.error = "That map position could not be converted to a coordinate.";
        updateStaticLocationPanel();
      } else {
        state.routeBuilder.status = "error";
        state.routeBuilder.error = "That map position could not be converted to a coordinate.";
        updateRouteBuilderPanel();
      }
      return;
    }
    const cartographic = Cesium.Cartographic.fromCartesian(cartesian);
    const point = {
      lat: Cesium.Math.toDegrees(cartographic.latitude),
      lon: Cesium.Math.toDegrees(cartographic.longitude)
    };
    if (staticPick) {
      const latitudeInput = document.querySelector(
        '[data-input="static-latitude"]'
      );
      const longitudeInput = document.querySelector(
        '[data-input="static-longitude"]'
      );
      if (latitudeInput) latitudeInput.value = point.lat.toFixed(7);
      if (longitudeInput) longitudeInput.value = point.lon.toFixed(7);
      state.staticLocation.draftPoint = point;
      state.staticLocation.pickMode = false;
      state.staticLocation.error = "";
      if (state.staticLocation.status === "error") {
        state.staticLocation.status = state.staticLocation.point
          ? "active"
          : "idle";
      }
      viewer.scene.canvas.style.cursor = "";
      syncStaticLocationTargetMarker();
      updateStaticLocationPanel();
      return;
    }
    setRouteBuilderEndpoint(
      target,
      point,
      target === "origin" ? "Map origin" : "Map destination"
    );
    state.routeBuilder.pickTarget = null;
    state.routeBuilder.status = "idle";
    viewer.scene.canvas.style.cursor = "";
    updateRouteBuilderPanel();
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
}

function syncRouteBuilderMarkers() {
  const viewer = state.viewer;
  if (!viewer || viewer.isDestroyed()) return;

  ["origin", "destination"].forEach(target => {
    const key = target === "origin" ? "builderOrigin" : "builderDestination";
    const point = state.routeBuilder[target];
    if (!point) {
      if (state.mapEntities[key]) {
        viewer.entities.remove(state.mapEntities[key]);
        delete state.mapEntities[key];
      }
      return;
    }
    const position = Cesium.Cartesian3.fromDegrees(point.lon, point.lat);
    const text = target === "origin" ? "DRAFT ORIGIN" : "DRAFT DEST";
    const color = target === "origin" ? "#f2c14e" : "#e78bd5";
    if (!state.mapEntities[key]) {
      state.mapEntities[key] = viewer.entities.add({
        position,
        point: {
          pixelSize: 11,
          color: Cesium.Color.fromCssColorString(color),
          outlineColor: Cesium.Color.fromCssColorString("#111820"),
          outlineWidth: 3,
          disableDepthTestDistance: Number.POSITIVE_INFINITY
        },
        label: {
          text,
          font: "700 11px -apple-system, BlinkMacSystemFont, sans-serif",
          fillColor: Cesium.Color.WHITE,
          showBackground: true,
          backgroundColor: Cesium.Color.fromCssColorString("#10161d").withAlpha(0.88),
          backgroundPadding: new Cesium.Cartesian2(8, 5),
          pixelOffset: new Cesium.Cartesian2(0, -26),
          disableDepthTestDistance: Number.POSITIVE_INFINITY
        }
      });
    } else {
      state.mapEntities[key].position = position;
    }
  });
  viewer.scene.requestRender();
}

function syncStaticLocationTargetMarker() {
  const viewer = state.viewer;
  if (!viewer || viewer.isDestroyed()) return;
  const point = state.staticLocation.draftPoint;
  if (!point) {
    if (state.mapEntities.staticTarget) {
      viewer.entities.remove(state.mapEntities.staticTarget);
      delete state.mapEntities.staticTarget;
      viewer.scene.requestRender();
    }
    return;
  }

  const position = Cesium.Cartesian3.fromDegrees(point.lon, point.lat);
  if (!state.mapEntities.staticTarget) {
    state.mapEntities.staticTarget = viewer.entities.add({
      position,
      point: {
        pixelSize: 11,
        color: Cesium.Color.fromCssColorString("#43a2d8"),
        outlineColor: Cesium.Color.WHITE,
        outlineWidth: 3,
        disableDepthTestDistance: Number.POSITIVE_INFINITY
      },
      label: {
        text: "STATIC TARGET",
        font: "700 11px -apple-system, BlinkMacSystemFont, sans-serif",
        fillColor: Cesium.Color.WHITE,
        showBackground: true,
        backgroundColor: Cesium.Color.fromCssColorString("#10161d").withAlpha(0.88),
        backgroundPadding: new Cesium.Cartesian2(8, 5),
        pixelOffset: new Cesium.Cartesian2(0, -26),
        disableDepthTestDistance: Number.POSITIVE_INFINITY
      }
    });
  } else {
    state.mapEntities.staticTarget.position = position;
  }
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

function toggleTrackSimulatedLocation() {
  state.trackSimulatedLocation = !state.trackSimulatedLocation;
  if (state.trackSimulatedLocation) {
    panToPoint(interpolatedPoint(), true);
  }
  updateLiveState();
}

function panToPhysicalLocation() {
  state.trackSimulatedLocation = false;
  if (state.physicalLocation.coords) {
    panToPoint({
      lat: state.physicalLocation.coords.lat,
      lon: state.physicalLocation.coords.lon
    }, true);
    updateLiveState();
    return;
  }

  state.physicalLocation.pendingPan = true;
  if (state.physicalLocation.watchId === null) {
    startPhysicalLocationWatch();
  } else {
    updateLiveState();
  }
}

function togglePhysicalLocationMarker() {
  if (state.physicalLocation.watchId !== null) {
    navigator.geolocation.clearWatch(state.physicalLocation.watchId);
    state.physicalLocation.watchId = null;
    state.physicalLocation.status = "idle";
    state.physicalLocation.coords = null;
    state.physicalLocation.error = "";
    state.physicalLocation.pendingPan = false;
    state.physicalLocation.pendingBuilderTarget = null;
    syncPhysicalLocationMarker();
    updateLiveState();
    return;
  }

  startPhysicalLocationWatch();
}

function startPhysicalLocationWatch() {
  if (!navigator.geolocation) {
    state.physicalLocation.status = "error";
    state.physicalLocation.error = "Browser Location Services are unavailable.";
    state.physicalLocation.pendingPan = false;
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
      if (state.physicalLocation.pendingBuilderTarget) {
        const target = state.physicalLocation.pendingBuilderTarget;
        state.physicalLocation.pendingBuilderTarget = null;
        setRouteBuilderEndpoint(
          target,
          {
            lat: position.coords.latitude,
            lon: position.coords.longitude
          },
          "Mac physical"
        );
      }
      if (state.physicalLocation.pendingPan) {
        state.physicalLocation.pendingPan = false;
        panToPoint({ lat: position.coords.latitude, lon: position.coords.longitude }, true);
      }
      updateLiveState();
    },
    error => {
      state.physicalLocation.status = "error";
      state.physicalLocation.error = locationErrorMessage(error);
      state.physicalLocation.pendingPan = false;
      if (state.physicalLocation.pendingBuilderTarget) {
        state.routeBuilder.status = "error";
        state.routeBuilder.error = state.physicalLocation.error;
        state.physicalLocation.pendingBuilderTarget = null;
      }
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

function degreesArray(points, height = 60) {
  if (!points.length) return [];
  const safePoints = points.length === 1 ? [points[0], points[0]] : points;
  return Cesium.Cartesian3.fromDegreesArrayHeights(
    safePoints.flatMap(point => [point.lon, point.lat, height])
  );
}

function frameRoute(animated, points = route()) {
  const viewer = state.viewer;
  if (!viewer || viewer.isDestroyed()) return;

  let minLat = Number.POSITIVE_INFINITY;
  let maxLat = Number.NEGATIVE_INFINITY;
  let minLon = Number.POSITIVE_INFINITY;
  let maxLon = Number.NEGATIVE_INFINITY;
  points.forEach(point => {
    minLat = Math.min(minLat, point.lat);
    maxLat = Math.max(maxLat, point.lat);
    minLon = Math.min(minLon, point.lon);
    maxLon = Math.max(maxLon, point.lon);
  });
  const centerLatitude = (minLat + maxLat) / 2;
  const longitudeScale = Math.max(
    0.2,
    Math.cos(centerLatitude * Math.PI / 180)
  );
  const latPad = Math.max((maxLat - minLat) * 0.13, 0.000015);
  const lonPad = Math.max(
    (maxLon - minLon) * 0.13,
    0.000015 / longitudeScale
  );
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

function panToPoint(point, animated) {
  const viewer = state.viewer;
  if (!viewer || viewer.isDestroyed() || !Number.isFinite(point.lat) || !Number.isFinite(point.lon)) return;

  const height = Math.max(viewer.camera.positionCartographic?.height || 14000, 25);
  const destination = Cesium.Cartesian3.fromDegrees(point.lon, point.lat, height);
  if (animated) {
    viewer.camera.flyTo({ destination, duration: 0.55 });
  } else {
    viewer.camera.setView({ destination });
  }
  viewer.scene.requestRender();
}

function coordinateText(point) {
  return `${point.lat.toFixed(7)}, ${point.lon.toFixed(7)}`;
}

async function copyCoordinates(source, button) {
  const point = source === "physical"
    ? state.physicalLocation.coords
    : interpolatedPoint();
  if (!point || !Number.isFinite(point.lat) || !Number.isFinite(point.lon)) return;

  try {
    const text = coordinateText(point);
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.append(textarea);
      textarea.select();
      const copied = document.execCommand("copy");
      textarea.remove();
      if (!copied) throw new Error("Clipboard copy failed.");
    }

    window.clearTimeout(button.copyFeedbackTimer);
    button.textContent = "COPIED";
    button.classList.add("copied");
    button.copyFeedbackTimer = window.setTimeout(() => {
      button.textContent = "COPY";
      button.classList.remove("copied");
    }, 1400);
  } catch (error) {
    button.textContent = "FAILED";
    window.setTimeout(() => {
      button.textContent = "COPY";
    }, 1400);
  }
}

function updateLiveState() {
  if (!state.points.length) return;

  const total = playbackDurationSeconds();
  const elapsed = total * state.progress;
  const partition = routeProgressPartition();
  const current = interpolatedPoint();
  const percent = Math.round(state.progress * 100);
  const backendPlayback = state.backend.playback || { state: "idle" };
  const device = state.backend.device;
  const deviceMessage = deviceProbeMessage(state.backend.deviceReport);
  const deviceOnline = isPhoneControllable();
  const activeRoutePhoneReady = canControlActiveRoute();
  const devicePlaybackActive = isDevicePlaybackActive();
  const importedPreview = state.direction === "imported";
  const preparedImport = importedPreview
    ? preparedRouteForImport(state.importedRoute.metadata)
    : null;
  const previewWithoutPhone = state.backend.available && !deviceOnline && !importedPreview;
  const backendError = state.backend.error;
  const staticActive = state.staticLocation.status === "active"
    && !!state.staticLocation.point;
  const status = state.backend.available && backendPlayback.state === "playing"
    ? `Phone traveling to ${destinationName()}`
    : state.backend.available && backendPlayback.state === "paused"
      ? "Phone route paused"
      : backendError
        ? "Backend needs attention"
      : staticActive
        ? "Static position active"
      : state.playing
        ? importedPreview
          ? "Previewing imported route"
          : `Previewing to ${destinationName()}`
        : state.progress >= 1
          ? importedPreview
            ? "Imported preview complete"
            : `Arrived at ${destinationName()}`
          : state.progress > 0
            ? "Paused"
            : importedPreview
              ? "Imported route ready to preview"
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
  setText("toggle-label", activeRoutePhoneReady || devicePlaybackActive
    ? backendPlayback.state === "playing"
      ? "Pause phone"
      : backendPlayback.state === "paused"
        ? "Resume phone"
        : "Start on phone"
    : state.playing
      ? "Pause preview"
      : state.progress > 0 && state.progress < 1
        ? "Resume preview"
        : previewWithoutPhone
          ? "Simulate path without phone"
          : importedPreview
            ? "Start route preview"
          : "Start preview");
  setText("progress-text", `${percent}% complete`);
  setText("progress-percent", `${percent}%`);
  setText("point-count", `${route().length}`);
  setText("map-source", importedPreview ? "IMPORTED GPX // WGS84" : "CESIUM // WGS84");
  setText("elapsed", durationText(elapsed));
  syncRemainingInput();
  setText("latitude", fmt.format(current.lat));
  setText("longitude", fmt.format(current.lon));
  setText("simulated-coordinate-copy", coordinateText(current));
  setText("physical-latitude", state.physicalLocation.coords ? fmt.format(state.physicalLocation.coords.lat) : "—");
  setText("physical-longitude", state.physicalLocation.coords ? fmt.format(state.physicalLocation.coords.lon) : "—");
  setText(
    "physical-coordinate-copy",
    state.physicalLocation.coords
      ? coordinateText(state.physicalLocation.coords)
      : "Location unavailable"
  );
  const physicalCopyButton = document.querySelector('[data-copy-source="physical"]');
  if (physicalCopyButton) {
    physicalCopyButton.disabled = !state.physicalLocation.coords;
  }
  setText("physical-location-label", state.physicalLocation.watchId !== null ? "HIDE MARKER" : "SHOW MARKER");
  setText("physical-location-note", state.physicalLocation.error
    ? state.physicalLocation.error
    : state.physicalLocation.status === "requesting"
      ? "Waiting for browser Location Services permission…"
      : state.physicalLocation.coords
        ? `Browser Location Services · accuracy ±${Math.round(state.physicalLocation.coords.accuracy)}m`
        : "Uses browser Location Services on this Mac, not simulated iPhone GPS.");
  setText("track-label", state.trackSimulatedLocation ? "TRACKING SIM" : "TRACK SIM");
  setText("control-state", state.backend.available
    ? backendPlayback.state === "playing"
      ? "DEVICE"
      : backendPlayback.state === "paused"
        ? "PAUSED"
        : state.playing
          ? "PREVIEW"
          : staticActive
            ? "FIXED"
          : importedPreview
            ? preparedImport
              ? "PHONE READY"
              : "IMPORTED"
          : previewWithoutPhone
            ? "NO PHONE"
            : "READY"
    : state.playing ? "RUNNING" : state.progress >= 1 ? "COMPLETE" : state.progress > 0 ? "PAUSED" : "STANDBY");
  setText("backend-note", state.backend.error
    ? state.backend.error
    : staticActive
      ? `iPhone fixed at ${coordinateText(state.staticLocation.point)}`
    : importedPreview
      ? preparedImport
        ? deviceOnline
          ? `Phone-ready track · ${durationMinutes(preparedImport.durationSeconds)}`
          : "Phone-ready track · connect an iPhone to play"
        : "Preview only · prepare this route for phone playback"
    : state.backend.available
      ? deviceOnline
        ? "Device controls are live"
        : state.playing
          ? "Preview simulation is running locally; no phone is being controlled."
          : deviceMessage || "Backend is running, but no USB iPhone is detected"
      : "Preview mode");
  setText(
    "transport-title",
    importedPreview && preparedImport ? "IPHONE TRANSPORT" : importedPreview ? "IMPORTED ROUTE" : deviceOnline ? "IPHONE TRANSPORT" : "PREVIEW TRANSPORT"
  );
  setText("device-name", deviceOnline ? device.DeviceName || "Connected iPhone" : state.backend.available ? "No iPhone detected" : "Backend offline");
  setText("device-model", deviceOnline ? device.ProductType || "iPhone" : "—");
  setText("device-ios", deviceOnline ? device.ProductVersion || "—" : "—");
  setText("device-detail", deviceOnline
    ? `${device.ConnectionType || "USB"} · ${device.Identifier || "paired device"}`
    : state.backend.available
      ? deviceMessage || "Connect and unlock the phone, then refresh status."
      : "Run ./scripts/run_frontend.sh for live phone control.");

  document.querySelectorAll(".status-pill:not(.connection-pill) .status-dot, .control-state .status-dot").forEach(dot => {
    dot.classList.toggle("playing", state.playing);
    dot.classList.toggle("complete", state.progress >= 1);
  });
  document.querySelector(".play-icon").textContent = state.playing ? "Ⅱ" : "▶";
  const toggleButton = document.querySelector('[data-action="toggle"]');
  if (toggleButton) {
    toggleButton.classList.toggle("preview-warning", previewWithoutPhone && !deviceOnline);
  }
  const trackButton = document.querySelector('[data-action="track-simulated"]');
  if (trackButton) {
    trackButton.classList.toggle("selected", state.trackSimulatedLocation);
    trackButton.setAttribute("aria-pressed", String(state.trackSimulatedLocation));
  }
  const physicalButton = document.querySelector('[data-action="pan-physical"]');
  if (physicalButton) {
    physicalButton.classList.toggle("pending", state.physicalLocation.pendingPan || state.physicalLocation.status === "requesting");
  }
  document.querySelectorAll("[data-direction]").forEach(button =>
    button.classList.toggle("selected", button.dataset.direction === state.direction)
  );
  renderImportedRouteList();
  document.querySelectorAll("[data-speed]").forEach(button =>
    button.classList.toggle("selected", Number(button.dataset.speed) === state.speed)
  );
  document.querySelectorAll(".connection-pill, [data-live-card='device']").forEach(node => {
    node.classList.toggle("online", deviceOnline);
    node.classList.toggle("backend-only", state.backend.available && !deviceOnline);
  });
  updateImportPanel();
  updateRouteBuilderPanel();
  updateStaticLocationPanel();

  const progress = document.querySelector(".progress");
  const bar = document.querySelector('[data-live="progress-bar"]');
  if (progress) progress.setAttribute("aria-valuenow", String(percent));
  if (bar) bar.style.width = `${state.progress * 100}%`;

  const viewer = state.viewer;
  if (viewer && !viewer.isDestroyed() && state.mapEntities.current) {
    state.mapEntities.current.position = Cesium.Cartesian3.fromDegrees(current.lon, current.lat);
    state.mapRouteGeometry.completed = degreesArray(partition.completed, 70);
    state.mapRouteGeometry.remaining = degreesArray(partition.remaining, 70);
    if (state.trackSimulatedLocation) {
      panToPoint(current, false);
    }
    viewer.scene.requestRender();
  }
}

function setText(key, value) {
  document.querySelectorAll(`[data-live="${key}"]`).forEach(node => {
    node.textContent = value;
  });
}

function setRouteText(key, value) {
  document.querySelectorAll(`[data-route-live="${key}"]`).forEach(node => {
    node.textContent = value;
  });
}

function syncRemainingInput() {
  const input = document.querySelector('[data-live-input="remaining-minutes"]');
  if (!input || state.previewRemainingInputFocused) return;
  const total = playbackDurationSeconds();
  const remaining = Math.max(0, total - total * state.progress);
  input.value = formatMinutesInput(remaining / 60);
  input.disabled = isDevicePlaybackActive();
  input.title = input.disabled
    ? "Phone playback uses the saved GPX timing. Stop phone playback before editing preview timing."
    : "Edit remaining preview minutes. At 0%, this is the total preview duration.";
}

function formatMinutesInput(minutes) {
  if (!Number.isFinite(minutes)) return "0";
  if (minutes >= 10) return String(Math.round(minutes));
  return String(Math.round(minutes * 10) / 10);
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
      const total = playbackDurationSeconds();
      if (total > 0) {
        const playbackRate = RouteProgress.playbackRateMultiplier(
          isDevicePlaybackActive(),
          state.speed
        );
        state.progress = Math.min(
          1,
          state.progress + ((now - state.lastFrame) / 1000) * playbackRate / total
        );
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
