package com.mukoo.logger;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.location.Location;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;

import java.time.Instant;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

// the drive session. a foreground service so sampling keeps running with the
// screen off in the car. each tick reads gps + serving-cell signal, writes one
// row to the local buffer, and every so often flushes the buffer to the api.
//
// data integrity across an OS kill: the active session id is persisted to disk
// on start. START_STICKY means the system restarts the service if it kills it;
// on that restart (null intent) we RESUME the same persisted session rather than
// starting a fresh one, so a killed-and-restarted drive stays one continuous
// session instead of silently fragmenting or dying. only an explicit user stop
// (ACTION_STOP) clears the persisted flag, so an OS kill always resumes.
public class DriveSessionService extends Service {

    public static final String EXTRA_SESSION_ID = "session_id";
    // sent by the UI to end a drive; distinguishes a user stop from an OS kill.
    public static final String ACTION_STOP = "com.mukoo.logger.action.STOP_DRIVE";

    // persisted so a sticky restart can resume the same session.
    private static final String PREFS = "drive_state";
    private static final String KEY_ACTIVE = "active";
    private static final String KEY_SESSION = "session_id";

    private static final String CHANNEL_ID = "drive_session";
    private static final int NOTIF_ID = 1;
    // ~3s. cell state changes over seconds, not milliseconds, so this cadence is
    // the biggest compute/battery saving we get for free. public so the map's live
    // layer refreshes in step with logging from one source of truth.
    public static final long SAMPLE_INTERVAL_MS = 3000L;
    // attempt an upload roughly every 30s of sampling.
    private static final int FLUSH_EVERY_N_SAMPLES = 10;

    // a cached last-known fix can be minutes old (tunnel, garage, cold start).
    // recording it would place the signal reading at the wrong spot, so ticks
    // with a fix older than this are skipped. public: the health line colours
    // its GPS age red at the same threshold, so the display and the guard agree.
    public static final long MAX_FIX_AGE_MS = 10_000L;

    // stationary thinning, tunable. sitting at a 2-minute red light at the 3s
    // cadence writes ~40 near-identical rows; instead, once we've moved less
    // than MIN_MOVE_METERS since the last kept sample we keep one sample per
    // STATIONARY_KEEP_INTERVAL_MS. trade-off: repeated stationary readings do
    // have averaging value, so flip THIN_WHEN_STATIONARY off to log every tick.
    // note this also thins a sub-12km/h crawl (a tick moves <10m there), which
    // for coverage mapping is the same "not really going anywhere" case.
    static final boolean THIN_WHEN_STATIONARY = true;
    static final float MIN_MOVE_METERS = 10f;
    static final long STATIONARY_KEEP_INTERVAL_MS = 30_000L;

    private String sessionId;
    private SampleStore store;
    private SignalReader signal;
    private LocationTracker location;
    private Uploader uploader;
    private PowerManager.WakeLock wakeLock;

    private HandlerThread workerThread;
    private Handler worker;
    private int tick = 0;
    private volatile boolean running = false;

    // uploads run on their own thread, never on the sampler's. over a marginal
    // rural link a flush can hang for the full connect+read timeout (~30s); if
    // it ran on the sampler thread that would silently stall the 3s cadence —
    // ~10 lost samples per hung batch, in exactly the terrain we're mapping.
    private ExecutorService uploadExecutor;
    // one flush in flight at a time; a tick that fires while one is running
    // just skips — the running flush already drains everything unsent.
    private final AtomicBoolean flushInFlight = new AtomicBoolean(false);

    // last KEPT sample, for stationary thinning.
    private Location lastKeptLoc;
    private long lastKeptElapsedMs;

    @Override
    public void onCreate() {
        super.onCreate();
        store = new SampleStore(this);
        signal = new SignalReader(this);
        location = new LocationTracker(this);
        uploader = new Uploader(this, store);
        createChannel();
        // sampling and db writes run off the main thread; uploads run off the
        // sampler thread too (see uploadExecutor).
        workerThread = new HandlerThread("mukoo-sampler");
        workerThread.start();
        worker = new Handler(workerThread.getLooper());
        uploadExecutor = Executors.newSingleThreadExecutor();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // explicit user stop: clear the persisted flag (so this does NOT resume)
        // and shut down. this is the only path that ends a drive for good.
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            persistActive(false);
            DriveState.onDriveStopped();
            running = false;
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
            return START_NOT_STICKY;
        }

        if (running) {
            return START_STICKY;
        }

        // pick the session id:
        //  - explicit id from the UI  -> new (or user-chosen) session
        //  - null intent (sticky restart) with a persisted active drive -> RESUME it
        //  - otherwise -> a fresh session
        String fromIntent = intent != null ? intent.getStringExtra(EXTRA_SESSION_ID) : null;
        if (fromIntent != null) {
            sessionId = fromIntent;
        } else if (isActive(this) && activeSession(this) != null) {
            sessionId = activeSession(this);
        } else {
            sessionId = UUID.randomUUID().toString();
        }
        persistActiveSession(sessionId);
        DriveState.onDriveStarted();

        startInForeground();
        acquireWakeLock();
        location.start();
        running = true;
        tick = 0;
        lastKeptLoc = null;
        lastKeptElapsedMs = 0L;
        worker.post(sampleLoop);
        return START_STICKY;
    }

    private final Runnable sampleLoop = new Runnable() {
        @Override
        public void run() {
            if (!running) {
                return;
            }
            takeSample();
            if (running) {
                worker.postDelayed(this, SAMPLE_INTERVAL_MS);
            }
        }
    };

    private void takeSample() {
        // the loop ran — even if this tick skips, the logger is not stalled.
        DriveState.onTickAlive();

        Location loc = location.getLast();
        // lat/lon are required by the schema; with no fix yet we cannot form a
        // valid sample, so skip this tick and wait for gps. a dead zone means no
        // serving cell, not no gps: gps keeps working with zero cellular coverage.
        if (loc == null) {
            return;
        }
        // stale-fix guard: both timestamps are elapsedRealtime-based, so this is
        // immune to wall-clock changes.
        long fixAgeMs = (SystemClock.elapsedRealtimeNanos() - loc.getElapsedRealtimeNanos())
                / 1_000_000L;
        if (fixAgeMs > MAX_FIX_AGE_MS) {
            return;
        }

        // stationary thinning: barely moved and recently sampled -> skip.
        long nowElapsed = SystemClock.elapsedRealtime();
        if (THIN_WHEN_STATIONARY && lastKeptLoc != null
                && loc.distanceTo(lastKeptLoc) < MIN_MOVE_METERS
                && (nowElapsed - lastKeptElapsedMs) < STATIONARY_KEEP_INTERVAL_MS) {
            return;
        }

        SignalReader.Reading r = signal.read();

        Sample s = new Sample();
        s.sampleId = UUID.randomUUID().toString();
        s.sessionId = sessionId;
        // stamp when we took the reading, not the gps fix's own time. a cached
        // last-known fix can be minutes old (cold start, tunnel), but the signal
        // measurement is happening right now.
        s.recordedAt = Instant.ofEpochMilli(System.currentTimeMillis()).toString();
        s.lat = loc.getLatitude();
        s.lon = loc.getLongitude();
        s.networkType = r.networkType;
        s.rsrp = r.rsrp;
        s.rsrq = r.rsrq;
        s.sinr = r.sinr;
        s.cellId = r.cellId;
        s.speedMps = loc.hasSpeed() ? (double) loc.getSpeed() : null;
        s.headingDeg = loc.hasBearing() ? normalizeBearing(loc.getBearing()) : null;

        store.insert(s);
        lastKeptLoc = new Location(loc);
        lastKeptElapsedMs = nowElapsed;
        // publish for the health line: logging is alive, last sample = now.
        DriveState.onSampleRecorded();

        tick++;
        if (tick % FLUSH_EVERY_N_SAMPLES == 0) {
            scheduleFlush();
        }
    }

    // hand the flush to the upload thread; sampling never waits on the network.
    private void scheduleFlush() {
        if (!flushInFlight.compareAndSet(false, true)) {
            return;
        }
        uploadExecutor.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    uploader.flush();
                } finally {
                    flushInFlight.set(false);
                }
            }
        });
    }

    // android bearing is already [0,360) but clamp defensively; the schema needs
    // heading_deg >= 0 and < 360.
    private static Double normalizeBearing(float b) {
        double d = b % 360.0;
        if (d < 0) {
            d += 360.0;
        }
        if (d >= 360.0) {
            d = 0.0;
        }
        return d;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) {
            worker.removeCallbacks(sampleLoop);
        }
        if (uploadExecutor != null) {
            // one last flush so a finished drive uploads promptly if online; it
            // completes on the upload thread after onDestroy returns (the process
            // outlives the service), then the executor winds down.
            uploadExecutor.execute(new Runnable() {
                @Override
                public void run() {
                    uploader.flush();
                }
            });
            uploadExecutor.shutdown();
        }
        location.stop();
        releaseWakeLock();
        if (workerThread != null) {
            workerThread.quitSafely();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void startInForeground() {
        Notification n = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("Mukoo drive logging")
                .setContentText("Recording cellular coverage")
                .setSmallIcon(android.R.drawable.ic_menu_mylocation)
                .setOngoing(true)
                .build();
        startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION);
    }

    private void createChannel() {
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, "Drive logging", NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("Active while a drive session is recording");
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.createNotificationChannel(ch);
        }
    }

    private void acquireWakeLock() {
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "mukoo:drive");
            wakeLock.acquire();
        }
    }

    private void releaseWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        wakeLock = null;
    }

    // --- persisted active-drive state, so a sticky restart resumes the same
    //     session. commit() (not apply()) on these: they must be on disk before a
    //     possible kill, and the writes are tiny. ---

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean isActive(Context c) {
        return prefs(c).getBoolean(KEY_ACTIVE, false);
    }

    static String activeSession(Context c) {
        return prefs(c).getString(KEY_SESSION, null);
    }

    private void persistActiveSession(String sessionId) {
        prefs(this).edit().putBoolean(KEY_ACTIVE, true).putString(KEY_SESSION, sessionId).commit();
    }

    private void persistActive(boolean active) {
        prefs(this).edit().putBoolean(KEY_ACTIVE, active).commit();
    }
}
