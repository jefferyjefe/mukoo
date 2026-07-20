package com.mukoo.logger.map;

import org.osmdroid.views.MapView;

import java.util.List;

// Prediction/active-learning layer: the ranked drive suggestions, as numbered
// pins. This is the layer the map was structured to accept — it slots onto the
// marked seam between history (below) and live (above) via the MapLayer
// interface, touching neither. Its data comes from the model package by way of
// SuggestionRepository (server GeoJSON, cached locally for offline); this class
// only turns a List<Suggestion> into pins and owns its overlay's lifecycle.
public class SuggestionLayer implements MapLayer {

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

    // Hand the overlay a fresh, pre-flattened set of pins. Safe to call off the
    // UI thread (e.g. after a network refresh); follow with MapView.postInvalidate().
    public void setSuggestions(List<Suggestion> suggestions) {
        final int n = suggestions.size();
        final double[] lat = new double[n];
        final double[] lon = new double[n];
        final int[] rank = new int[n];
        for (int i = 0; i < n; i++) {
            Suggestion s = suggestions.get(i);
            lat[i] = s.lat;
            lon[i] = s.lon;
            rank[i] = s.rank;
        }
        overlay.setSuggestions(lat, lon, rank);
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
}
