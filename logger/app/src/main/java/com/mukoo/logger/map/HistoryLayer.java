package com.mukoo.logger.map;

import com.mukoo.logger.SampleStore;

import org.osmdroid.views.MapView;

import java.util.List;

// History layer: every past sample as an RSRP-coloured dot, so driven roads are
// visible and gaps in coverage are obvious by eye. Source is local SQLite
// (SampleStore) — chosen over a backend read endpoint so the map works in the
// field with no connectivity. It owns a HistoryOverlay and the SQLite -> colour
// transform; it does not touch the map camera or the live reading.
public class HistoryLayer implements MapLayer {

    private final HistoryOverlay overlay;

    public HistoryLayer(float density) {
        this.overlay = new HistoryOverlay(density);
    }

    @Override
    public void attach(MapView map) {
        map.getOverlays().add(overlay);
    }

    @Override
    public void detach(MapView map) {
        map.getOverlays().remove(overlay);
    }

    // Read all samples from SQLite and hand the overlay a fresh, pre-coloured
    // point set. Runs the query + transform on the caller's thread — call it off
    // the UI thread — then invalidate the map to repaint. Cheap to re-run: it's
    // how the current drive's just-written samples show up as history.
    public void load(SampleStore store) {
        List<SampleStore.GeoSample> samples = store.allSamplesForMap();
        final int n = samples.size();
        final double[] lat = new double[n];
        final double[] lon = new double[n];
        final int[] color = new int[n];
        for (int i = 0; i < n; i++) {
            SampleStore.GeoSample s = samples.get(i);
            lat[i] = s.lat;
            lon[i] = s.lon;
            color[i] = RsrpColor.forSample(s.rsrp, s.networkType);
        }
        overlay.setPoints(lat, lon, color);
    }

    public int size() {
        return overlay.size();
    }
}
