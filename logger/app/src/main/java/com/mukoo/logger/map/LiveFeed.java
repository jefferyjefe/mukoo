package com.mukoo.logger.map;

import android.content.Context;
import android.location.Location;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;

import com.mukoo.logger.DriveSessionService;
import com.mukoo.logger.LocationTracker;
import com.mukoo.logger.SignalReader;

import java.util.concurrent.CopyOnWriteArrayList;

// The single live signal+location source for anything that displays live data.
// Both the embedded glance map and the full-screen map consume this one class,
// so there is exactly one signal-reading path and one cadence — never a forked
// reader per view.
//
// It owns one SignalReader + one LocationTracker, ticks at the same interval the
// logger samples at (DriveSessionService.SAMPLE_INTERVAL_MS), reads off a worker
// thread, and delivers each reading to its listeners on the main thread. It does
// NOT write to the sample store — display only; recording stays in the service.
//
// One instance per screen is fine: only one screen is resumed at a time
// (start() on resume, stop() on pause), so at most one feed is ticking. It is
// the code path that is shared, not a global singleton.
public class LiveFeed {

    // Delivered on the main thread. loc may be null before the first GPS fix.
    public interface Listener {
        void onLive(Location loc, SignalReader.Reading reading);
    }

    private final SignalReader signal;
    private final LocationTracker location;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final CopyOnWriteArrayList<Listener> listeners = new CopyOnWriteArrayList<>();

    private HandlerThread thread;
    private Handler worker;
    private volatile boolean running = false;

    public LiveFeed(Context context) {
        this.signal = new SignalReader(context);
        this.location = new LocationTracker(context);
    }

    public void addListener(Listener l) {
        listeners.add(l);
    }

    public void removeListener(Listener l) {
        listeners.remove(l);
    }

    public void start() {
        if (running) {
            return;
        }
        running = true;
        thread = new HandlerThread("mukoo-livefeed");
        thread.start();
        worker = new Handler(thread.getLooper());
        location.start();
        worker.post(tick);
    }

    public void stop() {
        running = false;
        if (worker != null) {
            worker.removeCallbacks(tick);
        }
        location.stop();
        if (thread != null) {
            thread.quitSafely();
            thread = null;
            worker = null;
        }
    }

    // worker thread: read the radio + last fix, then fan out to listeners on the
    // main thread. same cadence as logging, one source of truth.
    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (!running) {
                return;
            }
            final SignalReader.Reading reading = signal.read();
            final Location loc = location.getLast();
            main.post(() -> {
                for (Listener l : listeners) {
                    l.onLive(loc, reading);
                }
            });
            if (running) {
                worker.postDelayed(this, DriveSessionService.SAMPLE_INTERVAL_MS);
            }
        }
    };
}
