# Central Blue Route Controller

A local macOS development tool for preparing and playing timed GPX routes on a
tethered physical iPhone or iPad.

The project contains the complete Phase 3 route-building workflow from
[the build specification](docs/BUILD_SPEC.md):

- Inspect local Xcode, Python, and `pymobiledevice3` prerequisites.
- Convert Xcode waypoint GPX (`wpt`) into timed track GPX (`trkpt`).
- Validate the L1 → L2 and L2 → L1 route halves.
- Produce safe, dry-run-first playback and clear commands.
- Run a loopback-only backend for frontend-driven iPhone or iPad playback.
- Import arbitrary GPX geometry and prepare it for physical-device playback.
- Apply source or road-aware timing so highway and local-road segments retain
  different relative speeds.
- Build road-following routes from coordinates or coordinate-based Google Maps
  directions links.
- Run saved weekly location schedules with exact coordinates, local-time windows,
  overnight spans, and selected weekday recurrence.

The CLI does not modify a connected device unless `--execute` is explicitly
provided.

## Run the local controller

From the repository root:

```bash
./scripts/run_frontend.sh
```

Then open:

```text
http://localhost:8765/
```

This starts the loopback-only Python backend and serves the Cesium frontend.
When the backend is running, the start, pause, stop, and clear controls operate
on the selected tethered device through `pymobiledevice3`.

## Connect an iPhone or iPad

The initial supported device scope is USB-connected iPhone and iPad hardware
running iOS/iPadOS 17.4 or later. Unlock the device, approve **Trust This
Computer**, enable Developer Mode, and mount its matching developer image:

```bash
uv run --extra device pymobiledevice3 lockdown pair
uv run --extra device pymobiledevice3 amfi developer-mode-status
uv run --extra device pymobiledevice3 amfi reveal-developer-mode
```

Run `reveal-developer-mode` only when the status is `false` and the Developer
Mode toggle is absent. Then open **Settings > Privacy & Security > Developer
Mode** on the device, enable it, restart, and confirm the post-restart prompt.
Finally mount the developer image:

```bash
uv run --extra device pymobiledevice3 mounter auto-mount
```

Central Blue discovers devices with `usbmux list --usb`. One compatible device
is selected automatically. When multiple compatible devices are connected,
choose the intended device from the **Device Link** selector before using set,
play, or clear. Every command is then pinned to that device's UDID; disconnecting
it blocks new commands instead of falling back to another connected device.

See [the physical iPad acceptance test](docs/IPAD_COMPATIBILITY.md#physical-ipad-acceptance-test)
before promoting a particular iPad model and iPadOS build from expected
compatibility to physically verified support.

The frontend loads the GPX route into a Cesium map backed by OpenStreetMap.
No Cesium ion access token is required, but the map needs an internet
connection to load Cesium and map tiles. If you open `frontend/index.html`
without the backend, the controls remain in local preview mode.

## Import a GPX route

The loopback backend accepts JSON-first GPX imports:

```text
POST /api/routes/import-gpx
Content-Type: application/json

{
  "filename": "mapstogpx-route.gpx",
  "content": "<?xml version=\"1.0\"?>..."
}
```

The endpoint validates the filename, XML structure, coordinates, and route
geometry, then stores the source file in `routes/imports/`. It accepts GPX
tracks, routes, or waypoint collections with at least two points. Timestamps
are optional so MapsToGPX exports can be uploaded as-is.

The response includes the saved filename, point and segment counts, start/end
coordinates, map bounds, and whether every point has a timestamp. Imported
files are local runtime data and are ignored by Git.

The frontend exposes this through **Choose GPX File** under the **Import GPX**
accordion. Saved imports remain listed there after a page refresh and can be
selected like the built-in Home and Work routes. Selecting one immediately
frames it on the Cesium map. Untimed routes receive distance-proportional
20-minute timing for the local preview, which can be adjusted through the
remaining-minutes control.

Each saved import has a **Delete** control. After confirmation, deletion removes
the saved source GPX, its generated playback track, and its custom route-registry
entry. Deletion is blocked while that route is active on the device and cannot be
undone.

The left route library is organized into **Home and Work**, **Import GPX**, and
**Coordinates** accordions. Live device information appears in the top banner
so route and coordinate controls no longer compete with device status for
vertical space. Multiple route-library sections can remain open at the same
time, and the full left rail scrolls when their combined content is taller than
the window.

Select an imported route and press **Prepare**. **Use Best Available Travel
Time** is enabled by default:

- A fully timestamped GPX preserves its original relative speed profile.
- An untimed GPX sends distance-sampled route anchors to OSRM and applies the
  returned road-leg durations to the original GPX geometry.
- If a custom duration is selected, the complete profile is scaled while
  preserving the relative highway and local-road speeds.
- If OSRM is unavailable and a custom duration was supplied, preparation falls
  back to uniform distance timing and labels that fallback in the UI.

Provider-estimated timing requires internet access. The local MVP uses the
public OSRM demonstration endpoint by default and sends it up to 90
distance-spaced coordinates from the imported route. That endpoint is a
best-effort development dependency, not a production guarantee. It can be
replaced with a self-hosted or authorized compatible endpoint:

```bash
ROUTE_CONTROLLER_OSRM_URL=https://your-osrm.example ./scripts/run_frontend.sh
```

The controller status response advertises route-aware timing and deletion
capabilities. If the frontend was refreshed while an older backend process was
still running, it asks for a controller restart instead of sending an
unsupported nullable-duration request.

The backend generates a single fixed-cadence half-second `pymobiledevice3`
track under `routes/generated/` and registers it as a custom route. Dense or
duplicate source timestamps are resampled into strictly increasing playback
timestamps while retaining the exact route endpoints and relative speed
profile. Only successfully prepared imports become eligible for **Start on
device**. Preparing the same import again rebuilds its track and timing profile.

## Build a route from coordinates

Open **Build Route** in the route library to create road-following geometry
without first exporting a GPX file. Each endpoint can come from:

- **SIM** — the current simulated device position shown by the app.
- **MAC** — browser Location Services on the Mac.
- **MAP** — the next point clicked on the Cesium map.
- Manually entered decimal latitude and longitude.

Press **Generate Preview** to request full driving geometry and ETA from OSRM.
The resulting timed GPX is saved with the imported routes and opened in Cesium.
Review it, then press **Prepare** under **Import GPX** to create the half-second
device playback track. Route generation alone never starts or changes the
connected device's location.

Requested origin and destination pins retain seven decimal places and remain
visible separately from OSRM's road-snapped route endpoints. When endpoints are
close together, the map uses a tighter adaptive frame and permits closer zoom
instead of applying the wide framing used for city-scale routes.

The configured OSRM endpoint receives the selected origin and destination.
Central Blue does not log those coordinates. A generated route retains OSRM's
relative edge timing, and a custom duration scales that profile while
preserving faster and slower road sections.

## Activate a static simulated position

Open **Coordinates** and use **Set Simulated Position** under the live simulated
readout. Enter decimal latitude and longitude, or press **Pick on Map** and
click a point on the Cesium map. A map click only fills the coordinate fields
and displays a **Static Target** marker; press **Activate** to send that location
to the device. The loopback backend validates the coordinate and sends the
static location to the selected iPhone or iPad through `pymobiledevice3`.

Static activation is disabled while a route is playing. Starting a route
replaces the static position, and **Clear Location** restores real GPS. The
controller treats the active coordinate as desired state: it reasserts the
same location by keeping one healthy developer session open, checks that
session every five seconds, automatically reconnects only if the command or
developer tunnel drops, and uses a macOS `caffeinate` assertion to prevent idle
system sleep for the life of the static session. Establishing or reconnecting
requires the device to be unlocked; an already-connected session is not
replaced merely because the device later locks. The Coordinates panel reports
whether the session is active or reconnecting and surfaces device-lock errors.

The most recent manual target is stored locally in
`routes/schedules/static-location.json`, which is ignored by Git. A controller
restart restores that desired target and retries it when the selected device is
available. **Clear Location**, route playback, or schedule activation removes
the remembered manual target.

Keep the controller running and the iPad connected for an overnight session.
Closing the MacBook lid can still suspend a Mac unless it is in a supported
clamshell setup; the idle-sleep assertion is not a substitute for lid-open power.

## Build a route from a Google Maps link

In Google Maps, create directions whose origin and destination are explicit
latitude/longitude coordinates, copy the directions link, then paste it into
**Google Maps Directions Link** under **Build Route**. Central Blue supports:

- Full Maps URLs such as
  `https://www.google.com/maps/dir/?api=1&origin=37.4,-122.0&destination=37.5,-122.1`.
- Google-owned `maps.app.goo.gl` and legacy `goo.gl/maps` short links.

Press **Load Link**. The backend safely expands Google-owned redirects,
extracts the coordinate endpoints, requests fresh road geometry and ETA from
OSRM, saves the timed source GPX, and opens the preview. Review the route under
**Import GPX**, then press **Prepare** before starting it on the device.

The controller intentionally does not geocode place names or addresses. A link such as
“Stanford to Berkeley” produces an actionable message asking for a
coordinate-based link or manual endpoints. Central Blue does not scrape Google
route geometry, and pasting a link never starts device simulation.

Generated-route metadata is stored locally beside its GPX source, including
the provider, ETA, distance, requested endpoints, and source kind. Those
details remain available after refresh and are removed with the route.

## Run a repeating location schedule

Open **Location Schedule** and create one or more saved schedules. Use **New**
to start another definition, **Save** to retain it without changing the active
schedule, and the saved-schedule menu to switch between definitions.
Every window in a schedule contains:

- A label and exact latitude/longitude, entered manually or selected with
  **Pick Coordinates on Map**.
- A start and end time in the schedule's IANA timezone.
- The weekdays on which that window repeats.

Press **Save & Activate**. Only one saved schedule can be active at a time;
activating another atomically replaces the previous active schedule. The
controller immediately applies the window that
contains the current time, switches directly to the next window at its start,
and restores real GPS when no window is active. There is no interpolated travel
between scheduled locations. An end time earlier than its start is treated as
an overnight window whose repeat day is the day it starts. Overlapping windows
are rejected so the desired coordinate is always unambiguous.

The schedule library and its single active-schedule identifier are stored
locally in `routes/schedules/location-schedule.json`, which is ignored by Git.
Existing single-schedule files migrate automatically. The active schedule
resumes when the controller restarts; inactive schedules remain saved for later
selection. No language model, API key, geocoder, or routing request is used by
scheduling.

The controller holds a macOS `caffeinate` assertion for the entire time a
schedule is enabled, including gaps between windows. During an active window,
the persistent static-location watchdog holds the healthy session and
reconnects after command or tunnel failures. Keep the controller running,
the selected iPhone or iPad connected, and the Mac in a lid-open or supported
clamshell configuration.

Press **Stop Schedule** to disable recurrence and restore real GPS. An active
schedule must be stopped before it can be deleted. Starting a
route, manually activating a static coordinate, or clearing the location also
disables the saved schedule so it cannot unexpectedly take control again.

See [Phase 3 acceptance](docs/PHASE_3_ACCEPTANCE.md) for the automated checks
and the final tethered-device verification.

## Development setup

```bash
uv sync --extra dev
uv run route-controller verify
uv run route-controller convert routes/source/route_final.gpx routes/tracks
uv run pytest
```

`pymobiledevice3` is an optional device dependency:

```bash
uv sync --extra dev --extra device
uv run --extra device route-controller verify --probe-device
```

The probe is read-only. It lists USB-connected devices.

After a device is detected, preview a static-location command:

```bash
uv run --extra device route-controller set 37.3835546 -122.1371287
```

Add `--execute` only when you intentionally want to perform the device test,
then restore real location immediately afterward:

```bash
uv run --extra device route-controller set 37.3835546 -122.1371287 --execute
uv run --extra device route-controller clear --execute
```

If `pymobiledevice3` reports that it is trying `tunneld` on an iOS 17+
device, retry with the no-root userspace tunnel:

```bash
uv run --extra device route-controller set 37.3835546 -122.1371287 --userspace --execute
uv run --extra device route-controller clear --userspace --execute
```

## Inspect the generated routes

```bash
uv run route-controller inspect routes/tracks/route_L1_to_L2.track.gpx
uv run route-controller inspect routes/tracks/route_L2_to_L1.track.gpx
```

The source route contains 995 outbound points and 1,070 return points. The checked-in
playback tracks are interpolated at half-second intervals, so the outbound track
should report 2,933 points and the return track should report 2,441
points over 1,200 seconds.

## Playback safety

Preview the exact command first:

```bash
uv run --extra device route-controller play routes/tracks/route_L1_to_L2.track.gpx
```

Only after device discovery and static-location testing succeed, explicitly
run playback:

```bash
uv run --extra device route-controller play routes/tracks/route_L1_to_L2.track.gpx --execute
```

If the static-location proof needed `--userspace`, use it for route playback
too:

```bash
uv run --extra device route-controller play routes/tracks/route_L1_to_L2.track.gpx --userspace --execute
```

Restore the device's real location:

```bash
uv run --extra device route-controller clear --execute
```

On current `pymobiledevice3`, iOS 17.4+ normally uses an automatic no-root
userspace tunnel. Older iOS 17.0–17.3.1 devices may require privileged
`tunneld`; see the build specification before running privileged commands.
