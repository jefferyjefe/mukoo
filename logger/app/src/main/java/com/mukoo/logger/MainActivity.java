package com.mukoo.logger;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Outline;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewOutlineProvider;
import android.widget.Button;
import android.widget.TextView;

import com.mukoo.logger.map.LiveFeed;
import com.mukoo.logger.map.LiveMapController;
import com.mukoo.logger.map.LiveMetrics;
import com.mukoo.logger.map.OsmConfig;

import org.osmdroid.tileprovider.tilesource.TileSourceFactory;
import org.osmdroid.views.MapView;

import java.util.UUID;

// field ui: start/stop, live logged/uploaded counts, AND an always-visible glance
// map. the map is an extra panel, not a replacement — the controls stay put. the
// real logging work is still in DriveSessionService; the glance map only displays
// live position + signal, sharing the app's one LiveFeed. tap it for the full map.
public class MainActivity extends Activity {

    private static final int REQ_PERMS = 100;       // from Start: on grant, begin a drive
    private static final int REQ_PERMS_MINI = 101;  // up front: on grant, light up the map

    // requested up front. POST_NOTIFICATIONS is nice-to-have (the fgs notice);
    // the rest are essential to logging (and to the live map).
    private static final String[] REQUEST_PERMS = new String[]{
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
            Manifest.permission.READ_PHONE_STATE,
            Manifest.permission.POST_NOTIFICATIONS,
    };

    private Button toggle;
    private TextView loggedView;
    private TextView uploadedView;
    private TextView statusView;

    // embedded glance map: live layer only. shares the app's LiveFeed (one reader,
    // one cadence, one colour scale). tapping it opens the full MapActivity.
    private MapView miniMap;
    private LiveFeed liveFeed;
    private LiveMapController miniController;
    private TextView metricsMain;
    private TextView metricsSub;

    private SampleStore store;
    private boolean driving = false;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable refresh = new Runnable() {
        @Override
        public void run() {
            updateCounts();
            ui.postDelayed(this, 1000L);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // osmdroid must be configured before the MapView inflates.
        OsmConfig.apply(this);
        setContentView(R.layout.activity_main);
        store = new SampleStore(this);

        toggle = findViewById(R.id.toggle);
        loggedView = findViewById(R.id.logged);
        uploadedView = findViewById(R.id.uploaded);
        statusView = findViewById(R.id.status);
        metricsMain = findViewById(R.id.metricsMain);
        metricsSub = findViewById(R.id.metricsSub);

        toggle.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (driving) {
                    stopDrive();
                } else {
                    startDrive();
                }
            }
        });

        setupMiniMap();

        // request up front so the glance map is live without needing to tap Start.
        if (!hasEssentialPermissions()) {
            requestPermissions(REQUEST_PERMS, REQ_PERMS_MINI);
        }
    }

    private void setupMiniMap() {
        miniMap = findViewById(R.id.miniMap);
        miniMap.setTileSource(TileSourceFactory.MAPNIK);
        miniMap.setTilesScaledToDpi(true);
        miniMap.setMultiTouchControls(false);
        miniMap.getController().setZoom(16.0);

        // clip the square frame's map to a circle. setOval outline supports
        // clipping, so this is a clean circular glance map.
        miniMap.setOutlineProvider(new ViewOutlineProvider() {
            @Override
            public void getOutline(View view, Outline outline) {
                outline.setOval(0, 0, view.getWidth(), view.getHeight());
            }
        });
        miniMap.setClipToOutline(true);

        // the glance map doesn't pan/zoom; any tap opens the full detailed map.
        miniMap.setOnTouchListener((v, ev) -> {
            if (ev.getActionMasked() == MotionEvent.ACTION_UP) {
                v.performClick();
                startActivity(new Intent(MainActivity.this, MapActivity.class));
            }
            return true;
        });

        float density = getResources().getDisplayMetrics().density;
        liveFeed = new LiveFeed(this);
        miniController = new LiveMapController(miniMap, density,
                (loc, reading) -> LiveMetrics.render(metricsMain, metricsSub, loc, reading));
        liveFeed.addListener(miniController);
    }

    @Override
    protected void onResume() {
        super.onResume();
        ui.post(refresh);
        miniMap.onResume();
        liveFeed.start();
    }

    @Override
    protected void onPause() {
        super.onPause();
        ui.removeCallbacks(refresh);
        liveFeed.stop();
        miniMap.onPause();
    }

    private void startDrive() {
        if (!hasEssentialPermissions()) {
            requestPermissions(REQUEST_PERMS, REQ_PERMS);
            return;
        }
        String sessionId = UUID.randomUUID().toString();
        Intent i = new Intent(this, DriveSessionService.class);
        i.putExtra(DriveSessionService.EXTRA_SESSION_ID, sessionId);
        startForegroundService(i);
        driving = true;
        toggle.setText("Stop drive");
        statusView.setText("Recording session " + sessionId.substring(0, 8));
    }

    private void stopDrive() {
        stopService(new Intent(this, DriveSessionService.class));
        driving = false;
        toggle.setText("Start drive");
        statusView.setText("Stopped");
    }

    private void updateCounts() {
        loggedView.setText("Logged: " + store.countAll());
        uploadedView.setText("Uploaded: " + store.countUploaded());
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_PERMS) {
            // location + phone state are the ones logging actually needs.
            if (hasEssentialPermissions()) {
                startDrive();
            } else {
                statusView.setText("Location and phone permissions are required to log");
            }
        } else if (requestCode == REQ_PERMS_MINI) {
            // restart the feed so LocationTracker registers now that we're allowed.
            if (hasEssentialPermissions() && liveFeed != null) {
                liveFeed.stop();
                liveFeed.start();
            }
        }
    }

    // notifications are intentionally not "essential" here, so denying them never
    // traps startDrive in a re-request loop.
    private boolean hasEssentialPermissions() {
        return granted(Manifest.permission.ACCESS_FINE_LOCATION)
                && granted(Manifest.permission.READ_PHONE_STATE);
    }

    private boolean granted(String perm) {
        return checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED;
    }
}
