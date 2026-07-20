package com.mukoo.logger;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Outline;
import android.location.Location;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.text.Spannable;
import android.text.SpannableStringBuilder;
import android.text.style.ForegroundColorSpan;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewOutlineProvider;
import android.widget.Button;
import android.widget.TextView;

import com.mukoo.logger.map.LiveFeed;
import com.mukoo.logger.map.LiveMapController;
import com.mukoo.logger.map.LiveMetrics;
import com.mukoo.logger.map.OsmConfig;
import com.mukoo.logger.map.Suggestion;
import com.mukoo.logger.map.TargetArrowOverlay;

import org.osmdroid.tileprovider.tilesource.TileSourceFactory;
import org.osmdroid.views.MapView;

import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

// field ui: start/stop, live logged/uploaded counts, a glance map, AND an
// always-visible logging-health line so a stall is caught at a red light, not at
// home. the real logging work is in DriveSessionService; this screen displays it,
// sharing the app's one LiveFeed (no second signal/location reader).
public class MainActivity extends Activity {

    private static final int REQ_PERMS = 100;       // from Start: on grant, begin a drive
    private static final int REQ_PERMS_MINI = 101;  // up front: on grant, light up the map

    // health line colours: green = ok, red = act on it.
    private static final int HEALTH_OK = 0xFF2E7D32;
    private static final int HEALTH_ALERT = 0xFFD32F2F;

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
    private TextView healthView;

    // embedded glance map + its metrics, driven by the shared LiveFeed.
    private MapView miniMap;
    private LiveFeed liveFeed;
    private LiveMapController miniController;
    private TextView metricsMain;
    private TextView metricsSub;
    // edge chevron pointing at the nearest drive target; the glance map stays
    // live-only (no pins), this one arrow gives it direction without clutter.
    private TargetArrowOverlay targetArrow;
    private SuggestionRepository suggestionRepo;
    // the latest fix the LiveFeed handed us; the health line ages it each second.
    private volatile Location lastLiveLoc;

    private SampleStore store;
    private boolean driving = false;

    // post-drive buffer drain: store-and-forward's last leg. flushes during a
    // drive belong to the service; this covers "the stop-time flush failed and
    // rows are stranded" by retrying on app-open and whenever connectivity comes
    // back while the screen is open. drains skip while a drive is active — the
    // service is already flushing then.
    private Uploader uploader;
    private ExecutorService drainExec;
    private final ConnectivityManager.NetworkCallback drainOnNetwork =
            new ConnectivityManager.NetworkCallback() {
                @Override
                public void onAvailable(Network network) {
                    maybeDrain();
                }
            };

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable refresh = new Runnable() {
        @Override
        public void run() {
            refreshStatus();
            ui.postDelayed(this, 1000L);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        OsmConfig.apply(this);
        setContentView(R.layout.activity_main);
        store = new SampleStore(this);
        uploader = new Uploader(this, store);
        drainExec = Executors.newSingleThreadExecutor();

        toggle = findViewById(R.id.toggle);
        loggedView = findViewById(R.id.logged);
        uploadedView = findViewById(R.id.uploaded);
        statusView = findViewById(R.id.status);
        healthView = findViewById(R.id.health);
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

        miniMap.setOutlineProvider(new ViewOutlineProvider() {
            @Override
            public void getOutline(View view, Outline outline) {
                outline.setOval(0, 0, view.getWidth(), view.getHeight());
            }
        });
        miniMap.setClipToOutline(true);

        miniMap.setOnTouchListener((v, ev) -> {
            if (ev.getActionMasked() == MotionEvent.ACTION_UP) {
                v.performClick();
                startActivity(new Intent(MainActivity.this, MapActivity.class));
            }
            return true;
        });

        float density = getResources().getDisplayMetrics().density;
        liveFeed = new LiveFeed(this);
        miniController = new LiveMapController(miniMap, density, (loc, reading) -> {
            lastLiveLoc = loc;   // reused by the health line; no second reader
            if (loc != null && targetArrow != null) {
                targetArrow.setPosition(loc.getLatitude(), loc.getLongitude());
            }
            LiveMetrics.render(metricsMain, metricsSub, loc, reading);
        });
        liveFeed.addListener(miniController);

        // nearest-target chevron on top of the live layer. Data is the local
        // suggestion cache only (no network on this screen; the full map owns
        // refreshing), loaded off-thread on resume.
        targetArrow = new TargetArrowOverlay(density);
        miniMap.getOverlays().add(targetArrow);
        suggestionRepo = new SuggestionRepository(this);
    }

    @Override
    protected void onResume() {
        super.onResume();
        ui.post(refresh);
        miniMap.onResume();
        liveFeed.start();
        syncDrivingState();
        // glance arrow data: cached suggestions only, read off-thread.
        drainExec.execute(() -> {
            java.util.List<Suggestion> cached = suggestionRepo.loadCached();
            targetArrow.setTargets(cached);
            miniMap.postInvalidate();
        });
        // drain any stranded rows now, and again whenever a network shows up
        // while this screen is open.
        maybeDrain();
        ConnectivityManager cm = getSystemService(ConnectivityManager.class);
        if (cm != null) {
            cm.registerDefaultNetworkCallback(drainOnNetwork);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        ui.removeCallbacks(refresh);
        liveFeed.stop();
        miniMap.onPause();
        ConnectivityManager cm = getSystemService(ConnectivityManager.class);
        if (cm != null) {
            try {
                cm.unregisterNetworkCallback(drainOnNetwork);
            } catch (IllegalArgumentException ignored) {
                // already unregistered; nothing to do.
            }
        }
    }

    @Override
    protected void onDestroy() {
        if (drainExec != null) {
            drainExec.shutdown();
        }
        super.onDestroy();
    }

    private void maybeDrain() {
        drainExec.execute(() -> {
            // during a drive the service owns flushing; the server dedupes by
            // sample_id anyway, but there's no point racing it.
            if (!DriveState.isDriving() && store.countUnsent() > 0) {
                uploader.flush();
            }
        });
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

        // the usual cause of a dead background drive: prompt to exempt from
        // battery optimization (once; only while not already exempt).
        if (!BatteryOptimization.isExempt(this)) {
            promptBatteryExemption();
        }
    }

    private void stopDrive() {
        // explicit stop via ACTION_STOP so the service clears its persisted flag
        // and does NOT resume — as opposed to an OS kill, which does resume.
        Intent i = new Intent(this, DriveSessionService.class);
        i.setAction(DriveSessionService.ACTION_STOP);
        startService(i);
        driving = false;
        toggle.setText("Start drive");
        statusView.setText("Stopped");
    }

    // once a second: refresh counts + the logging-health line.
    private void refreshStatus() {
        int total = store.countAll();
        int uploaded = store.countUploaded();
        loggedView.setText("Logged: " + total);
        uploadedView.setText("Uploaded: " + uploaded);
        healthView.setText(buildHealth(total - uploaded));
    }

    // GPS status + fix age · last-sample age (red past 2x the interval == stalled)
    // · unsent buffer count. built fresh each second so ages climb visibly.
    private CharSequence buildHealth(int unsent) {
        long now = SystemClock.elapsedRealtime();
        SpannableStringBuilder sb = new SpannableStringBuilder();

        Location loc = lastLiveLoc;
        if (loc == null) {
            appendSpan(sb, "GPS NO FIX", HEALTH_ALERT);
        } else {
            long ageMs = Math.max(0, now - loc.getElapsedRealtimeNanos() / 1_000_000L);
            long ageS = ageMs / 1000L;
            // red at the same threshold the logger stops trusting the fix, so
            // the display and the sampler's stale-fix guard always agree.
            if (ageMs > DriveSessionService.MAX_FIX_AGE_MS) {
                appendSpan(sb, "GPS STALE " + ageS + "s", HEALTH_ALERT);
            } else {
                appendSpan(sb, "GPS ok " + ageS + "s", HEALTH_OK);
            }
        }
        sb.append(" · ");

        if (!DriveState.isDriving()) {
            sb.append("smp idle");
        } else {
            // STALL keys off the sampler LOOP being alive, not off rows written:
            // stationary thinning and stale-fix skips are intentional pauses, and
            // must not read as a dead logger at a red light.
            long lastTick = DriveState.lastTickElapsedMs();
            boolean stalled = lastTick != 0L
                    && (now - lastTick) > 2 * DriveSessionService.SAMPLE_INTERVAL_MS;
            long last = DriveState.lastSampleElapsedMs();
            String age = last == 0L ? "—" : (Math.max(0, (now - last) / 1000L) + "s");
            if (stalled) {
                appendSpan(sb, "smp " + age + " STALL", HEALTH_ALERT);
            } else {
                sb.append("smp ").append(age);
            }
        }
        sb.append(" · ");

        sb.append("buf ").append(Integer.toString(unsent));
        return sb;
    }

    private static void appendSpan(SpannableStringBuilder sb, String text, int color) {
        int start = sb.length();
        sb.append(text);
        sb.setSpan(new ForegroundColorSpan(color), start, sb.length(),
                Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
    }

    // reflect an active drive in the button/status when returning to the screen
    // (e.g. reopened mid-drive, or the service resumed after an OS restart).
    private void syncDrivingState() {
        driving = DriveState.isDriving();
        toggle.setText(driving ? "Stop drive" : "Start drive");
        if (driving) {
            CharSequence s = statusView.getText();
            if (s == null || !s.toString().startsWith("Recording")) {
                statusView.setText("Recording");
            }
        }
    }

    private void promptBatteryExemption() {
        new AlertDialog.Builder(this)
                .setTitle("Keep logging alive")
                .setMessage("For a long unattended drive, allow Mukoo to ignore battery "
                        + "optimization so Android doesn't kill logging in the background.")
                .setPositiveButton("Allow", (d, w) -> BatteryOptimization.request(this))
                .setNegativeButton("Not now", null)
                .show();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_PERMS) {
            if (hasEssentialPermissions()) {
                startDrive();
            } else {
                statusView.setText("Location and phone permissions are required to log");
            }
        } else if (requestCode == REQ_PERMS_MINI) {
            if (hasEssentialPermissions() && liveFeed != null) {
                liveFeed.stop();
                liveFeed.start();
            }
        }
    }

    private boolean hasEssentialPermissions() {
        return granted(Manifest.permission.ACCESS_FINE_LOCATION)
                && granted(Manifest.permission.READ_PHONE_STATE);
    }

    private boolean granted(String perm) {
        return checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED;
    }
}
