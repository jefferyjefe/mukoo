package com.mukoo.logger.map;

import com.mukoo.logger.SignalReader;

import org.osmdroid.util.GeoPoint;
import org.osmdroid.views.MapView;

// Live layer: the current position, coloured by the current serving-cell RSRP.
// It reads nothing itself — the activity drives it on the sampling cadence with a
// fix + a SignalReader.Reading (the exact same reader the logger records with),
// so the map's live colour and the logged data can never disagree. Owns only the
// marker overlay; the always-visible metrics panel is the activity's text view.
public class LiveLayer implements MapLayer {

    private final LiveOverlay overlay;

    public LiveLayer(float density) {
        this.overlay = new LiveOverlay(density);
    }

    @Override
    public void attach(MapView map) {
        map.getOverlays().add(overlay);
    }

    @Override
    public void detach(MapView map) {
        map.getOverlays().remove(overlay);
    }

    // Place + colour the marker from one tick's fix and reading.
    public void update(double lat, double lon, SignalReader.Reading reading) {
        overlay.update(new GeoPoint(lat, lon), RsrpColor.forSample(reading.rsrp, reading.networkType));
    }

    public boolean hasFix() {
        return overlay.hasFix();
    }
}
