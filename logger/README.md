# logger

The Android field-logger app. It records cellular signal measurements while
driving rural routes and uploads them in batches to the ingestion API's
`POST /v1/measurements` endpoint.

Native Android, Java, no third-party dependencies — it uses only platform APIs
(`TelephonyManager`, `LocationManager`, SQLite, `org.json`, `HttpURLConnection`),
so the APK is tiny and there is nothing to resolve or break in the field.

## Design notes

- **Client-generated `sample_id`** (UUID) per measurement, so uploads are
  idempotent and safe to retry over flaky links — the server dedupes on it.
- **`session_id`** (UUID) per drive, sent once at the batch level.
- **Dead zones are data.** When there is no serving cell, we still emit a sample
  with `network_type = "none"` and null `rsrp`/`rsrq`/`sinr`. Absence of coverage
  is the primary signal this project maps. GPS still works with zero cellular
  coverage, so dead-zone samples still carry a real lat/lon.
- **Store-and-forward.** Every sample lands in local SQLite first. Batches flush
  to the API when a link is available; offline just means the buffer grows until
  one comes back. Rows are marked uploaded only on an HTTP 200.
- **Slow sampling is the big saving.** We sample every ~3s. Cell state changes
  over seconds, not milliseconds, so a low cadence costs almost nothing and still
  captures everything that matters.
- **Only store a reading that changed.** Even at 3s we outrun the modem, which
  refreshes its signal report far more slowly — across the first 3,130 samples,
  ~86% of stored rows re-reported the previous reading. `SignalChangeGate` drops
  those: a row is stored when a metric moves, when the network type changes
  (entering a dead zone is never thinned away), when the serving cell changes, or
  every 30s regardless so a steady drive still leaves a spatial trace. Replayed
  over those 3,130 real samples it keeps 502 — a 6.2x cut, almost all of it while
  moving, since stationary thinning already handled the parked case.
  Each stored sample also carries `modem_reported_at`, the modem's own timestamp,
  so a re-read is identifiable at ingestion instead of inferred from equal values.
- **Thinning bounds modem polls; the gate bounds rows.** Stationary thinning keys
  off the last time we *read* the modem, not the last row stored — otherwise it
  would stop suppressing as soon as the gate started dropping reads, and parking
  for an hour would poll the modem ~1200 times instead of ~120.

## How it fits together

- `MainActivity` — the whole UI: a start/stop button and live counts of samples
  logged vs uploaded. It just toggles the service and polls the local db.
- `DriveSessionService` — a foreground service (so sampling survives the screen
  turning off in the car). Each tick reads GPS + the serving-cell signal, stores a
  row if the reading changed, and periodically flushes the buffer.
- `SignalReader` — pulls RSRP/RSRQ/SINR, network type, cell id and the modem's
  own reading timestamp off the modem.
- `SignalChangeGate` — decides whether a reading is new enough to store. Plain
  object, no Android dependencies, so the logic is unit-tested on the JVM.
- `LocationTracker` — holds the latest GPS fix (lat, lon, speed, heading).
- `SampleStore` — the SQLite store-and-forward buffer.
- `Uploader` — drains the buffer to `POST /v1/measurements`, one session-batch
  at a time. Carrier is `Verizon` for now.

## Build and install

Needs a JDK (Android Studio's bundled JBR is fine) and the Android SDK, with
`adb` seeing the phone (`adb devices`). Built and verified against compileSdk 36,
minSdk 33, on a Pixel 7a.

```bash
cd logger

# point at the android sdk (edit if yours differs)
echo "sdk.dir=$HOME/Library/Android/sdk" > local.properties

# build the debug apk
JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" \
  ./gradlew :app:assembleDebug

# install on the connected phone (or: ./gradlew installDebug)
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Tap **Start drive**, grant location + phone permissions when asked, and drive.
The counters update once a second.

## Pointing it at the ingestion API

The upload URL is baked in at build time via `MUKOO_BASE_URL` (defaults to
`http://127.0.0.1:8000`).

**Bench test over USB** — run the API on your laptop and forward the port so the
phone's localhost reaches it:

```bash
adb reverse tcp:8000 tcp:8000
```

**Real drive** — build with your server's LAN or public address:

```bash
./gradlew :app:assembleDebug -PMUKOO_BASE_URL=http://192.168.1.20:8000
```

Cleartext HTTP is allowed (`usesCleartextTraffic="true"`) for talking to a local
ingest box; put TLS in front of it before anything leaves a lab.

## Scope

A field tool, not a product: minimal UI, one carrier, debug builds installed by
hand over adb. This app is just the collection front end for the pipeline that
lives in the rest of the repo.
