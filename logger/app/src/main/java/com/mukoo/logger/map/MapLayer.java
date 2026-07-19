package com.mukoo.logger.map;

import org.osmdroid.views.MapView;

// A self-contained visual layer on the map. The activity owns a MapView and a
// list of MapLayers, attaching them in z-order (history under live). Each layer
// owns its own osmdroid overlay(s) and its own data source; nothing about one
// layer's rendering or data leaks into another.
//
// This is the seam that keeps a future prediction/uncertainty layer a drop-in:
// implement MapLayer, attach it between history and live, and the rest of the
// map — camera follow, the live cadence loop, the metrics panel — is untouched.
public interface MapLayer {

    // Add this layer's overlay(s) to the map. Called once, in z-order.
    void attach(MapView map);

    // Remove this layer's overlay(s). Called on teardown.
    void detach(MapView map);
}
