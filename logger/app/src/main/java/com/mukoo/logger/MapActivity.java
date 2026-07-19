package com.mukoo.logger;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.view.MotionEvent;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

import com.mukoo.logger.map.HistoryLayer;
import com.mukoo.logger.map.LiveFeed;
import com.mukoo.logger.map.LiveMapController;
import com.mukoo.logger.map.LiveMetrics;
import com.mukoo.logger.map.OsmConfig;

import org.osmdroid.tileprovider.cachemanager.CacheManager;
import org.osmdroid.tileprovider.tilesource.ITileSource;
import org.osmdroid.tileprovider.tilesource.OnlineTileSourceBase;
import org.osmdroid.tileprovider.tilesource.TileSourceFactory;
import org.osmdroid.util.BoundingBox;
import org.osmdroid.views.MapView;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

// The full-screen detailed map: history + live, drawn in z-order, with camera
// follow and a metrics panel. The live half comes from the shared LiveFeed /
// LiveMapController / LiveMetrics — the same path the embedded glance map on the
// main screen uses — so there is one cadence, one colour scale, one reader. This
// screen adds what the glance view omits: the history layer and free panning.
//
// Layers attach bottom-up: history, then live (LiveMapController attaches the
// live layer). A prediction/uncertainty layer slots in on the marked line
// between them without touching the feed, camera, or panel.
public class MapActivity extends Activity {

    private static final int REQ_PERMS = 200;
    // re-read history from SQLite every N ticks so a running drive's samples show
    // up as the trail behind you. N * cadence ≈ 9s.
    private static final int HISTORY_REFRESH_EVERY_TICKS = 3;
    // offline-cache guard: refuse a download bigger than this many tiles, both to
    // stay polite to the OSM tile servers and to keep one tap from queueing a
    // multi-hundred-MB pull. zoom in and cache in passes instead.
    private static final int MAX_CACHE_TILES = 3000;
    // street-level detail; matches the default viewing zoom of 16.
    private static final int CACHE_ZOOM_MAX = 16;

    private MapView map;
    private TextView metricsMain;
    private TextView metricsSub;
    private Button recenter;

    private HistoryLayer historyLayer;
    private LiveFeed liveFeed;
    private LiveMapController controller;

    private SampleStore store;
    private ExecutorService io;
    private int ticks = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        OsmConfig.apply(this);
        setContentView(R.layout.activity_map);

        map = findViewById(R.id.map);
        metricsMain = findViewById(R.id.metricsMain);
        metricsSub = findViewById(R.id.metricsSub);
        recenter = findViewById(R.id.recenter);

        map.setTileSource(TileSourceFactory.MAPNIK);
        map.setMultiTouchControls(true);
        map.setTilesScaledToDpi(true);
        map.getController().setZoom(16.0);

        float density = getResources().getDisplayMetrics().density;
        store = new SampleStore(this);
        io = Executors.newSingleThreadExecutor();

        // ---- ATTACH-ORDER: bottom (history) up to top (live) ----
        historyLayer = new HistoryLayer(density);
        historyLayer.attach(map);
        // prediction/uncertainty layer would attach here (above history, below live)
        controller = new LiveMapController(map, density,
                (loc, reading) -> LiveMetrics.render(metricsMain, metricsSub, loc, reading));

        liveFeed = new LiveFeed(this);
        liveFeed.addListener(controller);
        liveFeed.addListener(historyRefresh);

        // manual pan drops follow (only a real touch fires ACTION_MOVE); Recenter
        // re-locks.
        map.setOnTouchListener((v, ev) -> {
            if (ev.getActionMasked() == MotionEvent.ACTION_MOVE && controller.isFollowing()) {
                controller.setFollowing(false);
                updateRecenterLabel();
            }
            return false;
        });
        recenter.setOnClickListener(v -> {
            controller.recenter();
            updateRecenterLabel();
        });
        updateRecenterLabel();

        findViewById(R.id.cacheArea).setOnClickListener(v -> cacheVisibleArea());

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
        // initial history paint from local SQLite, off the UI thread.
        io.execute(() -> {
            historyLayer.load(store);
            map.postInvalidate();
        });
        liveFeed.start();
    }

    @Override
    protected void onPause() {
        super.onPause();
        liveFeed.stop();
        map.onPause();
    }

    @Override
    protected void onDestroy() {
        if (io != null) {
            io.shutdownNow();
        }
        super.onDestroy();
    }

    // periodic history reload, driven off the shared feed's cadence but done on
    // the io thread so the SQLite read never touches the UI thread.
    private final LiveFeed.Listener historyRefresh = (loc, reading) -> {
        ticks++;
        if (ticks % HISTORY_REFRESH_EVERY_TICKS == 0) {
            io.execute(() -> {
                historyLayer.load(store);
                map.postInvalidate();
            });
        }
    };

    // pre-download the visible box's tiles, from the current zoom down to street
    // level, into osmdroid's tile cache. once cached, the map draws offline —
    // which matters because dead zones are exactly where this tool gets used.
    private void cacheVisibleArea() {
        // OSM's Mapnik source encodes a no-bulk-download policy and CacheManager
        // enforces it by THROWING INSIDE ITS ASYNCTASK — uncatchable from here,
        // it kills the process. so check the policy first and, when bulk isn't
        // allowed, point at the compliant path instead: tiles cache as you view
        // them and OsmConfig keeps them for ~a month, so panning the planned
        // route once while online is the pre-cache.
        ITileSource src = map.getTileProvider().getTileSource();
        if (!(src instanceof OnlineTileSourceBase)
                || !((OnlineTileSourceBase) src).getTileSourcePolicy().acceptsBulkDownload()) {
            Toast.makeText(this,
                    "OSM tiles don't allow bulk download — pan the route once while "
                            + "online instead; viewed tiles stay cached for ~a month",
                    Toast.LENGTH_LONG).show();
            return;
        }
        BoundingBox box = map.getBoundingBox();
        int zoomMin = Math.min((int) map.getZoomLevelDouble(), CACHE_ZOOM_MAX);
        CacheManager cacheManager = new CacheManager(map);
        int tiles = cacheManager.possibleTilesInArea(box, zoomMin, CACHE_ZOOM_MAX);
        if (tiles > MAX_CACHE_TILES) {
            Toast.makeText(this,
                    "Area too large (" + tiles + " tiles) — zoom in and cache in passes",
                    Toast.LENGTH_LONG).show();
            return;
        }
        Toast.makeText(this, "Caching " + tiles + " tiles for offline…",
                Toast.LENGTH_SHORT).show();
        cacheManager.downloadAreaAsync(this, box, zoomMin, CACHE_ZOOM_MAX,
                new CacheManager.CacheManagerCallback() {
                    @Override
                    public void onTaskComplete() {
                        Toast.makeText(MapActivity.this,
                                "Offline cache complete", Toast.LENGTH_SHORT).show();
                    }

                    @Override
                    public void onTaskFailed(int errors) {
                        Toast.makeText(MapActivity.this,
                                "Cache finished, " + errors + " tiles failed",
                                Toast.LENGTH_LONG).show();
                    }

                    @Override
                    public void updateProgress(int progress, int currentZoomLevel,
                                               int zoomMin, int zoomMax) {
                        // quiet: toasts per tile would be spam in a car.
                    }

                    @Override
                    public void downloadStarted() {
                    }

                    @Override
                    public void setPossibleTilesInArea(int total) {
                    }
                });
    }

    private void updateRecenterLabel() {
        recenter.setText(controller.isFollowing() ? "Following" : "Recenter");
    }

    private boolean hasLivePermissions() {
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_PERMS && hasLivePermissions()) {
            // restart the feed so LocationTracker registers now that we're allowed.
            liveFeed.stop();
            liveFeed.start();
        }
    }
}
