package com.mukoo.logger.map;

import org.osmdroid.views.MapView;

import java.util.List;

// Prediction/active-learning layer: the ranked drive suggestions, as numbered
// pins. This is the layer the map was structured to accept — it slots onto the
// marked seam between history (below) and live (above) via the MapLayer
// interface, touching neither. Its data comes from the model package by way of
// SuggestionRepository (server GeoJSON, cached locally for offline); this class
// turns a List<Suggestion> into pins, owns its overlay's lifecycle, and tracks
// which targets this drive has already covered (they fade) from the live fix.
public class SuggestionLayer implements MapLayer {

    // Passing within this range of a target counts as having sampled it: the
    // logger records continuously, so driving by IS the measurement. ~150 m
    // matches the surface's cell size, so "covered" means "that cell got data".
    private static final double COVER_RADIUS_M = 150.0;

    private final SuggestionOverlay overlay;

    public SuggestionLayer(float density) {
        this.overlay = new SuggestionOverlay(density);
    }

    @Override
    public void attach(MapView map) {
        map.getOverlays().add(overlay);
    }

    @Override
    public void detach(MapView map) {
        map.getOverlays().remove(overlay);
    }

    // Tap = show details, long-press = navigate; the activity decides.
    public void setListener(SuggestionOverlay.Listener listener) {
        overlay.setListener(listener);
    }

    // Hand the overlay a fresh set of pins. Safe to call off the UI thread
    // (e.g. after a network refresh); follow with MapView.postInvalidate().
    public void setSuggestions(List<Suggestion> suggestions) {
        overlay.setSuggestions(suggestions.toArray(new Suggestion[0]));
    }

    // Feed the live position each tick; targets within COVER_RADIUS_M become
    // covered (sticky for this map session) and fade. Returns true when
    // something newly faded, so the caller knows to repaint.
    public boolean updateLive(double lat, double lon) {
        Suggestion[] items = overlay.items();
        boolean changed = false;
        for (int i = 0; i < items.length; i++) {
            if (distanceM(lat, lon, items[i].lat, items[i].lon) <= COVER_RADIUS_M) {
                changed |= overlay.markCovered(i);
            }
        }
        return changed;
    }

    // Equirectangular approximation — exact enough at 150 m scales, and cheap
    // enough to run every sampling tick.
    static double distanceM(double lat1, double lon1, double lat2, double lon2) {
        double latRad = Math.toRadians((lat1 + lat2) / 2.0);
        double dLat = Math.toRadians(lat2 - lat1);
        double dLon = Math.toRadians(lon2 - lon1) * Math.cos(latRad);
        double r = 6371000.0;
        return r * Math.sqrt(dLat * dLat + dLon * dLon);
    }

    // Toggle visibility without detaching: a disabled overlay is skipped by
    // osmdroid's draw pass, so the pins vanish/return with one repaint.
    public void setVisible(boolean visible) {
        overlay.setEnabled(visible);
    }

    public boolean isVisible() {
        return overlay.isEnabled();
    }

    public int size() {
        return overlay.size();
    }

    public int remaining() {
        return overlay.remaining();
    }
}
