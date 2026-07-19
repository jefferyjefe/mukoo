package com.mukoo.logger;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageManager;
import android.location.Location;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.view.MotionEvent;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

import com.mukoo.logger.map.HistoryLayer;
import com.mukoo.logger.map.LiveLayer;
import com.mukoo.logger.map.MapLayer;
import com.mukoo.logger.map.RsrpColor;

import org.osmdroid.config.Configuration;
import org.osmdroid.tileprovider.tilesource.TileSourceFactory;
import org.osmdroid.util.GeoPoint;
import org.osmdroid.views.MapView;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

// The map screen. Hosts one MapView and a stack of independent layers drawn in
// z-order, and drives the live layer on the same cadence the logger samples at.
//
// Layers are the extension seam. They are attached bottom-up:
//     history  (past samples, from local SQLite)
//     live     (current position + RSRP colour)
// A prediction/uncertainty layer slots in later as one more MapLayer added at
// ATTACH-ORDER below (above history, below live) — no change to the camera,
// the cadence loop, or the panel.
//
// The live reading comes from the very same SignalReader + LocationTracker the
// DriveSessionService logs with, so the colour on the map and the data on disk
// can't drift apart. This screen only displays; recording stays in the service.
public class MapActivity extends Activity {

    private static final int REQ_PERMS = 200;
    // re-read history from SQLite every N ticks so samples the running drive is
    // writing show up as the trail behind you. N*cadence ≈ 9s: fresh enough to
    // watch a road fill in, rare enough to stay cheap on a big dataset.
    private static final int HISTORY_REFRESH_EVERY_TICKS = 3;

    private MapView map;
    private TextView metricsMain;
    private TextView metricsSub;
    private Button recenter;

    private final List<MapLayer> layers = new ArrayList<>();
    private HistoryLayer historyLayer;
    private LiveLayer liveLayer;

    private SampleStore store;
    private SignalReader signal;
    private LocationTracker location;

    private HandlerThread workerThread;
    private Handler worker;
    private volatile boolean running = false;
    private int ticks = 0;

    private boolean following = true;
    private boolean initialCentered = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // osmdroid needs a real user agent before any tile fetch or OSM 403s us.
        // Give it its own prefs file so we don't pull in androidx.preference.
        Configuration.getInstance().load(
                getApplicationContext(), getSharedPreferences("osmdroid", Context.MODE_PRIVATE));
        Configuration.getInstance().setUserAgentValue(getPackageName());

        setContentView(R.layout.activity_map);

        map = findViewById(R.id.map);
        metricsMain = findViewById(R.id.metricsMain);
        metricsSub = findViewById(R.id.metricsSub);
        recenter = findViewById(R.id.recenter);

        map.setTileSource(TileSourceFactory.MAPNIK);   // OpenStreetMap, no API key
        map.setMultiTouchControls(true);
        map.setTilesScaledToDpi(true);                 // crisper labels = car-readable
        map.getController().setZoom(16.0);

        float density = getResources().getDisplayMetrics().density;

        store = new SampleStore(this);
        signal = new SignalReader(this);
        location = new LocationTracker(this);

        // ---- ATTACH-ORDER: bottom (history) up to top (live) ----
        historyLayer = new HistoryLayer(density);
        liveLayer = new LiveLayer(density);
        layers.add(historyLayer);
        // layers.add(predictionLayer);   // future 3rd layer drops in right here
        layers.add(liveLayer);
        for (MapLayer layer : layers) {
            layer.attach(map);
        }

        // manual pan turns off follow (only a real touch does — programmatic
        // recenters don't fire ACTION_MOVE). Recenter turns it back on.
        map.setOnTouchListener((v, ev) -> {
            if (ev.getActionMasked() == MotionEvent.ACTION_MOVE && following) {
                following = false;
                updateRecenterLabel();
            }
            return false; // let the map handle the gesture itself
        });
        recenter.setOnClickListener(v -> {
            following = true;
            updateRecenterLabel();
            Location loc = location.getLast();
            if (loc != null) {
                map.getController().animateTo(new GeoPoint(loc.getLatitude(), loc.getLongitude()));
            }
        });
        updateRecenterLabel();

        workerThread = new HandlerThread("mukoo-map");
        workerThread.start();
        worker = new Handler(workerThread.getLooper());

        if (!hasLivePermissions()) {
            requestPermissions(
                    new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
                            Manifest.permission.READ_PHONE_STATE},
                    REQ_PERMS);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        map.onResume();
        startLive();
    }

    @Override
    protected void onPause() {
        super.onPause();
        stopLive();
        map.onPause();
    }

    @Override
    protected void onDestroy() {
        running = false;
        if (worker != null) {
            worker.removeCallbacksAndMessages(null);
        }
        if (workerThread != null) {
            workerThread.quitSafely();
        }
        super.onDestroy();
    }

    private void startLive() {
        if (running) {
            return;
        }
        running = true;
        ticks = 0;
        location.start();
        // initial history paint, then begin the cadence loop.
        worker.post(() -> {
            historyLayer.load(store);
            runOnUiThread(() -> {
                map.invalidate();
                Toast.makeText(this,
                        "History: " + historyLayer.size() + " points",
                        Toast.LENGTH_SHORT).show();
            });
        });
        worker.post(tick);
    }

    private void stopLive() {
        running = false;
        if (worker != null) {
            worker.removeCallbacks(tick);
        }
        location.stop();
    }

    // one sampling tick, on the worker thread: read the radio + last fix off the
    // UI thread, refresh history periodically, then hand the UI a repaint.
    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (!running) {
                return;
            }
            final SignalReader.Reading reading = signal.read();
            final Location loc = location.getLast();

            ticks++;
            if (ticks % HISTORY_REFRESH_EVERY_TICKS == 0) {
                historyLayer.load(store);
            }

            runOnUiThread(() -> onTick(loc, reading));

            if (running) {
                worker.postDelayed(this, DriveSessionService.SAMPLE_INTERVAL_MS);
            }
        }
    };

    // UI thread: update the panel, move/recolour the live marker, follow.
    private void onTick(Location loc, SignalReader.Reading reading) {
        updatePanel(loc, reading);
        if (loc != null) {
            liveLayer.update(loc.getLatitude(), loc.getLongitude(), reading);
            GeoPoint here = new GeoPoint(loc.getLatitude(), loc.getLongitude());
            if (!initialCentered) {
                map.getController().setCenter(here);
                initialCentered = true;
            } else if (following) {
                map.getController().animateTo(here);
            }
        }
        map.invalidate();
    }

    private void updatePanel(Location loc, SignalReader.Reading r) {
        int color = RsrpColor.forSample(r.rsrp, r.networkType);
        String main;
        if ("none".equals(r.networkType)) {
            main = "NO SIGNAL";
        } else {
            main = r.networkType + "  " + fmt(r.rsrp) + " dBm";
        }
        metricsMain.setTextColor(panelTextColor(color));
        metricsMain.setText(main);

        if (loc == null) {
            metricsSub.setText("waiting for GPS…");
        } else {
            StringBuilder sub = new StringBuilder();
            sub.append("RSRQ ").append(fmt(r.rsrq))
               .append("   SINR ").append(fmt(r.sinr));
            if (r.cellId != null) {
                sub.append("   CID ").append(r.cellId);
            }
            metricsSub.setText(sub.toString());
        }
    }

    // black (dead zone) would vanish on the dark panel, so alert in red there;
    // every other bucket colour is legible on the panel as-is.
    private int panelTextColor(int rsrpColor) {
        return rsrpColor == RsrpColor.NO_SIGNAL ? 0xFFFF6E6E : rsrpColor;
    }

    private static String fmt(Double v) {
        return v == null ? "--" : Long.toString(Math.round(v));
    }

    private void updateRecenterLabel() {
        recenter.setText(following ? "Following" : "Recenter");
    }

    private boolean hasLivePermissions() {
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_PERMS && hasLivePermissions()) {
            // (re)start location now that we're allowed to read it.
            location.start();
        }
    }
}
