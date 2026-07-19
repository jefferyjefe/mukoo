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

    // keep viewed tiles for a month past their server expiry. OSM's tile policy
    // forbids bulk pre-download (osmdroid enforces it for the Mapnik source), so
    // the policy-compliant offline story is: browse the route once while online
    // and the tiles you saw stay cached — this makes them stay long enough to
    // cover a field campaign.
    private static final long TILE_EXPIRY_EXTENSION_MS = 30L * 24 * 60 * 60 * 1000;

    public static void apply(Context context) {
        Configuration.getInstance().load(
                context.getApplicationContext(),
                context.getSharedPreferences("osmdroid", Context.MODE_PRIVATE));
        Configuration.getInstance().setUserAgentValue(context.getPackageName());
        Configuration.getInstance().setExpirationExtendedDuration(TILE_EXPIRY_EXTENSION_MS);
    }
}
