package com.mukoo.logger.map;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Point;

import org.osmdroid.util.BoundingBox;
import org.osmdroid.util.GeoPoint;
import org.osmdroid.views.MapView;
import org.osmdroid.views.Projection;
import org.osmdroid.views.overlay.Overlay;

// Draws the ranked drive suggestions as numbered "go here next" pins: a fat
// accent-coloured teardrop whose tip marks the exact target, a white halo + dark
// rim so it reads on any tile, and the rank number in the head. Deliberately a
// different SHAPE and a different COLOUR from everything else on the map — the
// history layer is small RSRP-coloured dots, the live marker is a haloed RSRP
// dot, and these are big violet pins — so "where I've been", "where I am", and
// "where to go" never blur together at a glance in a moving car.
//
// Follows the HistoryOverlay pattern: one overlay drawing all pins straight to
// the canvas from parallel primitive arrays, swapped by reference so a refresh
// can hand off a new set without tearing. The set is tiny (~10), but keeping the
// same shape means the same culling and the same threading story.
public class SuggestionOverlay extends Overlay {

    // A vivid violet, chosen to sit outside the RSRP scale (green/yellow/orange/
    // red/black) and off OSM's own road/water palette, so a pin is never mistaken
    // for a signal reading.
    private static final int ACCENT = 0xFF7C4DFF;

    private volatile double[] lats = new double[0];
    private volatile double[] lons = new double[0];
    private volatile int[] ranks = new int[0];

    private final float coreR;
    private final float haloR;
    private final float stemLen;
    private final Paint halo = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint rim = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint stemWhite = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint stemFill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint number = new Paint(Paint.ANTI_ALIAS_FLAG);

    // reused across the draw loop to avoid per-pin allocation.
    private final Point screen = new Point();
    private final GeoPoint reuse = new GeoPoint(0.0, 0.0);
    private final Path stem = new Path();

    public SuggestionOverlay(float density) {
        this.coreR = 15f * density;          // big head — a 2-digit rank must fit
        this.haloR = coreR + 3f * density;
        this.stemLen = 11f * density;

        halo.setStyle(Paint.Style.FILL);
        halo.setColor(0xFFFFFFFF);
        fill.setStyle(Paint.Style.FILL);
        fill.setColor(ACCENT);
        rim.setStyle(Paint.Style.STROKE);
        rim.setStrokeWidth(1.5f * density);
        rim.setColor(0xFF202020);
        stemWhite.setStyle(Paint.Style.FILL);
        stemWhite.setColor(0xFFFFFFFF);
        stemFill.setStyle(Paint.Style.FILL);
        stemFill.setColor(ACCENT);

        number.setColor(0xFFFFFFFF);
        number.setTextAlign(Paint.Align.CENTER);
        number.setFakeBoldText(true);
        number.setTextSize(coreR * 1.15f);
    }

    // Swap in a new set of pins (parallel arrays, same length, treated as
    // immutable). Safe from a background thread; follow with MapView.postInvalidate().
    public void setSuggestions(double[] lat, double[] lon, int[] rank) {
        this.lats = lat;
        this.lons = lon;
        this.ranks = rank;
    }

    public int size() {
        return lats.length;
    }

    @Override
    public void draw(Canvas canvas, MapView mapView, boolean shadow) {
        if (shadow) {
            return;
        }
        final double[] la = lats;
        final double[] lo = lons;
        final int[] rk = ranks;
        final int n = Math.min(la.length, Math.min(lo.length, rk.length));
        if (n == 0) {
            return;
        }

        final Projection proj = mapView.getProjection();
        final BoundingBox box = proj.getBoundingBox();
        final double north = box.getLatNorth();
        final double south = box.getLatSouth();
        final double west = box.getLonWest();
        final double east = box.getLonEast();
        // pins are tall (head sits above the tip), so give generous slack so a pin
        // whose tip is just off the top edge still draws its head.
        final double latSlack = (north - south) * 0.15 + 1e-4;
        final double lonSlack = Math.abs(east - west) * 0.1 + 1e-4;

        // draw lowest priority first so rank 1 lands on top of any overlap.
        for (int i = n - 1; i >= 0; i--) {
            final double lat = la[i];
            final double lon = lo[i];
            if (lat > north + latSlack || lat < south - latSlack) {
                continue;
            }
            if (lon < west - lonSlack || lon > east + lonSlack) {
                continue;
            }
            reuse.setCoords(lat, lon);
            proj.toPixels(reuse, screen);
            drawPin(canvas, screen.x, screen.y, rk[i]);
        }
    }

    // one pin: tip at (tipX, tipY) = the target; head centred above it.
    private void drawPin(Canvas canvas, float tipX, float tipY, int rank) {
        final float headCy = tipY - stemLen - coreR;
        final float baseY = headCy + coreR * 0.55f; // base tucked inside the head
        final float baseHalf = coreR * 0.6f;

        // stem: a white triangle with a slightly smaller accent triangle on top,
        // so the pointer keeps a white edge like the head does. Base is hidden
        // under the opaque head, so only the pointing tip shows.
        stem.reset();
        stem.moveTo(tipX, tipY + 1.5f);
        stem.lineTo(tipX - baseHalf - 2f, baseY);
        stem.lineTo(tipX + baseHalf + 2f, baseY);
        stem.close();
        canvas.drawPath(stem, stemWhite);

        stem.reset();
        stem.moveTo(tipX, tipY);
        stem.lineTo(tipX - baseHalf, baseY);
        stem.lineTo(tipX + baseHalf, baseY);
        stem.close();
        canvas.drawPath(stem, stemFill);

        // head: white halo, accent fill, dark rim (mirrors the live marker's
        // legibility recipe), then the rank number.
        canvas.drawCircle(tipX, headCy, haloR, halo);
        canvas.drawCircle(tipX, headCy, coreR, fill);
        canvas.drawCircle(tipX, headCy, coreR, rim);

        final Paint.FontMetrics fm = number.getFontMetrics();
        final float baseline = headCy - (fm.ascent + fm.descent) / 2f;
        canvas.drawText(Integer.toString(rank), tipX, baseline, number);
    }
}
