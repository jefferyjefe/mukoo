package com.mukoo.logger.map;

import android.content.Context;

import org.osmdroid.config.Configuration;

// One place to initialise osmdroid. Must run before any MapView is inflated —
// both the embedded glance map (MainActivity) and the full map (MapActivity)
// call this, so the user agent (required or OSM tile servers 403) is set once,
// the same way, for every map in the app.
public final class OsmConfig {

    private OsmConfig() {
    }

    public static void apply(Context context) {
        Configuration.getInstance().load(
                context.getApplicationContext(),
                context.getSharedPreferences("osmdroid", Context.MODE_PRIVATE));
        Configuration.getInstance().setUserAgentValue(context.getPackageName());
    }
}
