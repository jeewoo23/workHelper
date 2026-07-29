# iPhone Route Controller

A local macOS development tool for preparing and playing timed GPX routes on a
tethered physical iPhone.

The project is currently implementing Phase 2 from
[the build specification](docs/BUILD_SPEC.md):

- Inspect local Xcode, Python, and `pymobiledevice3` prerequisites.
- Convert Xcode waypoint GPX (`wpt`) into timed track GPX (`trkpt`).
- Validate the L1 → L2 and L2 → L1 route halves.
- Produce safe, dry-run-first playback and clear commands.
- Run a loopback-only backend for frontend-driven iPhone playback.

The CLI does not modify a connected phone unless `--execute` is explicitly
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
on the tethered iPhone through `pymobiledevice3`.

The frontend loads the GPX route into a Cesium map backed by OpenStreetMap.
No Cesium ion access token is required, but the map needs an internet
connection to load Cesium and map tiles. If you open `frontend/index.html`
without the backend, the controls remain in local preview mode.

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
uv run route-controller verify --probe-device
```

The probe is read-only. It lists USB-connected devices.

After a device is detected, preview a static-location command:

```bash
uv run route-controller set 37.3835546 -122.1371287
```

Add `--execute` only when you intentionally want to perform the device test,
then restore real location immediately afterward:

```bash
uv run route-controller set 37.3835546 -122.1371287 --execute
uv run route-controller clear --execute
```

If `pymobiledevice3` reports that it is trying `tunneld` on an iOS 17+
device, retry with the no-root userspace tunnel:

```bash
uv run route-controller set 37.3835546 -122.1371287 --userspace --execute
uv run route-controller clear --userspace --execute
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
uv run route-controller play routes/tracks/route_L1_to_L2.track.gpx
```

Only after device discovery and static-location testing succeed, explicitly
run playback:

```bash
uv run route-controller play routes/tracks/route_L1_to_L2.track.gpx --execute
```

If the static-location proof needed `--userspace`, use it for route playback
too:

```bash
uv run route-controller play routes/tracks/route_L1_to_L2.track.gpx --userspace --execute
```

Restore the phone's real location:

```bash
uv run route-controller clear --execute
```

On current `pymobiledevice3`, iOS 17.4+ normally uses an automatic no-root
userspace tunnel. Older iOS 17.0–17.3.1 devices may require privileged
`tunneld`; see the build specification before running privileged commands.
