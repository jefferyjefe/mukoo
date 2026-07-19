package com.mukoo.logger.map;

import android.location.Location;

import com.mukoo.logger.SignalReader;

import org.osmdroid.util.GeoPoint;
import org.osmdroid.views.MapView;

// Wires a MapView's live layer to a LiveFeed: on each tick it moves + recolours
// the current-position marker and (optionally) updates a metrics panel, and
// keeps the camera on the user. Reused by both the glance map and the full map,
// so "current position, coloured by RSRP, following me" is implemented once.
//
// It owns only the live layer. History (and, later, a prediction layer) are
// attached separately by the screen that wants them, underneath this one.
public class LiveMapController implements LiveFeed.Listener {

    // Lets a screen render the reading into its own panel (or ignore it).
    public interface MetricsSink {
        void show(Location loc, SignalReader.Reading reading);
    }

    private final MapView map;
    private final LiveLayer liveLayer;
    private final MetricsSink metrics; // nullable

    private boolean following = true;
    private boolean initialCentered = false;
    private Location lastLoc;

    public LiveMapController(MapView map, float density, MetricsSink metrics) {
        this.map = map;
        this.metrics = metrics;
        this.liveLayer = new LiveLayer(density);
        this.liveLayer.attach(map);
    }

    @Override
    public void onLive(Location loc, SignalReader.Reading reading) {
        lastLoc = loc;
        if (metrics != null) {
            metrics.show(loc, reading);
        }
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

    public void setFollowing(boolean following) {
        this.following = following;
    }

    public boolean isFollowing() {
        return following;
    }

    // re-lock onto the user and jump there now.
    public void recenter() {
        following = true;
        if (lastLoc != null) {
            map.getController().animateTo(new GeoPoint(lastLoc.getLatitude(), lastLoc.getLongitude()));
        }
    }
}
