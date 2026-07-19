package com.mukoo.logger.map;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Point;

import org.osmdroid.util.BoundingBox;
import org.osmdroid.util.GeoPoint;
import org.osmdroid.views.MapView;
import org.osmdroid.views.Projection;
import org.osmdroid.views.overlay.Overlay;

// Renders the whole recorded history as coloured dots. Deliberately NOT one
// osmdroid Marker per sample: a long drive is thousands of points, and Markers
// would allocate and lay out a View each. Instead this is a single overlay that
// draws every visible point straight to the canvas in one pass, culling anything
// off-screen, so cost scales with what's on screen, not with the dataset size.
//
// Data is held as parallel primitive arrays (no per-point object) and swapped
// atomically by reference, so a background reload can hand off a fresh set while
// the UI thread is mid-draw without tearing.
public class HistoryOverlay extends Overlay {

    private volatile double[] lats = new double[0];
    private volatile double[] lons = new double[0];
    private volatile int[] colors = new int[0];

    private final float radiusPx;
    private final float deadRadiusPx;
    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint ring = new Paint(Paint.ANTI_ALIAS_FLAG);

    // reused across the draw loop to avoid per-point allocation.
    private final Point screen = new Point();
    private final GeoPoint reuse = new GeoPoint(0.0, 0.0);

    public HistoryOverlay(float density) {
        this.radiusPx = 3.5f * density;      // ~3.5dp dot — visible but not blobby
        this.deadRadiusPx = 4.5f * density;  // dead zones a touch larger to stand out
        fill.setStyle(Paint.Style.FILL);
        ring.setStyle(Paint.Style.STROKE);
        ring.setStrokeWidth(Math.max(1f, density));
        ring.setColor(0x66FFFFFF);           // faint white edge so dots read on any tile
    }

    // Swap in a new point set. Arrays must be the same length and are treated as
    // immutable once passed. Safe to call from a background thread; follow with
    // MapView.postInvalidate().
    public void setPoints(double[] lat, double[] lon, int[] color) {
        this.lats = lat;
        this.lons = lon;
        this.colors = color;
    }

    public int size() {
        return lats.length;
    }

    @Override
    public void draw(Canvas canvas, MapView mapView, boolean shadow) {
        if (shadow) {
            return;
        }
        // snapshot the references once — a reload may swap them mid-draw.
        final double[] la = lats;
        final double[] lo = lons;
        final int[] co = colors;
        final int n = Math.min(la.length, Math.min(lo.length, co.length));
        if (n == 0) {
            return;
        }

        final Projection proj = mapView.getProjection();
        final BoundingBox box = proj.getBoundingBox();
        final double north = box.getLatNorth();
        final double south = box.getLatSouth();
        final double west = box.getLonWest();
        final double east = box.getLonEast();
        // a little slack so points at the very edge don't blink in and out.
        final double latSlack = (north - south) * 0.05 + 1e-4;
        final double lonSlack = Math.abs(east - west) * 0.05 + 1e-4;

        for (int i = 0; i < n; i++) {
            final double lat = la[i];
            final double lon = lo[i];
            if (lat > north + latSlack || lat < south - latSlack) {
                continue;
            }
            // regional tool: ignore antimeridian wrap.
            if (lon < west - lonSlack || lon > east + lonSlack) {
                continue;
            }
            reuse.setCoords(lat, lon);
            proj.toPixels(reuse, screen);
            final int color = co[i];
            final float r = (color == RsrpColor.NO_SIGNAL) ? deadRadiusPx : radiusPx;
            fill.setColor(color);
            canvas.drawCircle(screen.x, screen.y, r, fill);
            canvas.drawCircle(screen.x, screen.y, r, ring);
        }
    }
}
