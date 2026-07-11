package com.mukoo.logger;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.location.Location;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.PowerManager;

import java.time.Instant;
import java.util.UUID;

// the drive session. a foreground service so sampling keeps running with the
// screen off in the car. each tick reads gps + serving-cell signal, writes one
// row to the local buffer, and every so often flushes the buffer to the api.
public class DriveSessionService extends Service {

    public static final String EXTRA_SESSION_ID = "session_id";

    private static final String CHANNEL_ID = "drive_session";
    private static final int NOTIF_ID = 1;
    // ~3s. cell state changes over seconds, not milliseconds, so this cadence is
    // the biggest compute/battery saving we get for free.
    private static final long SAMPLE_INTERVAL_MS = 3000L;
    // attempt an upload roughly every 30s of sampling.
    private static final int FLUSH_EVERY_N_SAMPLES = 10;

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

    @Override
    public void onCreate() {
        super.onCreate();
        store = new SampleStore(this);
        signal = new SignalReader(this);
        location = new LocationTracker(this);
        uploader = new Uploader(this, store);
        createChannel();
        // all sampling, db writes and uploads run off the main thread.
        workerThread = new HandlerThread("mukoo-sampler");
        workerThread.start();
        worker = new Handler(workerThread.getLooper());
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (running) {
            return START_STICKY;
        }
        sessionId = intent != null ? intent.getStringExtra(EXTRA_SESSION_ID) : null;
        if (sessionId == null) {
            sessionId = UUID.randomUUID().toString();
        }

        startInForeground();
        acquireWakeLock();
        location.start();
        running = true;
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
        Location loc = location.getLast();
        // lat/lon are required by the schema; with no fix yet we cannot form a
        // valid sample, so skip this tick and wait for gps. a dead zone means no
        // serving cell, not no gps: gps keeps working with zero cellular coverage.
        if (loc == null) {
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

        tick++;
        if (tick % FLUSH_EVERY_N_SAMPLES == 0) {
            uploader.flush();
        }
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
            // one last flush so a finished drive uploads promptly if online.
            worker.post(new Runnable() {
                @Override
                public void run() {
                    uploader.flush();
                }
            });
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
}
