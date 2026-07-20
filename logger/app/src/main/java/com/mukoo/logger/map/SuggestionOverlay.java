package com.mukoo.logger.map;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Point;
import android.view.MotionEvent;

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
// Pins covered by this drive (the layer marks them as you pass within range)
// fade to a ghost so remaining targets pop. Tapping a pin reports it to the
// Listener (the activity shows an info card); long-pressing reports separately
// (the activity hands off to navigation).
//
// Follows the HistoryOverlay pattern: one overlay drawing all pins straight to
// the canvas, references swapped atomically so a refresh can hand off a new set
// without tearing. N is ~10, so the draw loop over objects is nothing.
public class SuggestionOverlay extends Overlay {

    // A vivid violet, chosen to sit outside the RSRP scale (green/yellow/orange/
    // red/black) and off OSM's own road/water palette, so a pin is never mistaken
    // for a signal reading.
    private static final int ACCENT = 0xFF7C4DFF;
    private static final int FADE_ALPHA = 0x50; // covered pins: ghosted, still legible

    public interface Listener {
        void onTap(Suggestion s);

        void onLongPress(Suggestion s);
    }

    private volatile Suggestion[] items = new Suggestion[0];
    private volatile boolean[] covered = new boolean[0];
    private volatile Listener listener;

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

    public void setListener(Listener listener) {
        this.listener = listener;
    }

    // Swap in a new set of pins. Resets covered state: a fresh suggestion set
    // means fresh targets. Safe from a background thread; follow with
    // MapView.postInvalidate().
    public void setSuggestions(Suggestion[] suggestions) {
        this.items = suggestions;
        this.covered = new boolean[suggestions.length];
    }

    public Suggestion[] items() {
        return items;
    }

    // Mark one pin as covered by this drive (sticky). Returns true if it was
    // not already covered, so the caller knows a repaint is worth it.
    public boolean markCovered(int index) {
        boolean[] c = covered;
        if (index < 0 || index >= c.length || c[index]) {
            return false;
        }
        c[index] = true;
        return true;
    }

    public int size() {
        return items.length;
    }

    public int remaining() {
        Suggestion[] it = items;
        boolean[] c = covered;
        int n = 0;
        for (int i = 0; i < it.length && i < c.length; i++) {
            if (!c[i]) {
                n++;
            }
        }
        return n;
    }

    @Override
    public void draw(Canvas canvas, MapView mapView, boolean shadow) {
        if (shadow) {
            return;
        }
        final Suggestion[] it = items;
        final boolean[] cov = covered;
        if (it.length == 0) {
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
        for (int i = it.length - 1; i >= 0; i--) {
            final Suggestion s = it[i];
            if (s.lat > north + latSlack || s.lat < south - latSlack) {
                continue;
            }
            if (s.lon < west - lonSlack || s.lon > east + lonSlack) {
                continue;
            }
            reuse.setCoords(s.lat, s.lon);
            proj.toPixels(reuse, screen);
            final boolean ghost = i < cov.length && cov[i];
            setAlpha(ghost ? FADE_ALPHA : 0xFF);
            drawPin(canvas, screen.x, screen.y, s.rank);
        }
        setAlpha(0xFF); // leave paints opaque for the next draw pass
    }

    private void setAlpha(int alpha) {
        halo.setAlpha(alpha);
        fill.setAlpha(alpha);
        rim.setAlpha(alpha);
        stemWhite.setAlpha(alpha);
        stemFill.setAlpha(alpha);
        number.setAlpha(alpha);
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

    // -- touch -----------------------------------------------------------

    // Highest-priority pin whose head or tip is under the touch, or -1. Head
    // centre sits stemLen+coreR above the tip in screen space; a generous
    // radius keeps it hittable from a car.
    private int hit(MotionEvent e, MapView mapView) {
        final Suggestion[] it = items;
        if (it.length == 0) {
            return -1;
        }
        final Projection proj = mapView.getProjection();
        final float slop = haloR * 1.35f;
        for (int i = 0; i < it.length; i++) { // ascending rank: topmost pin wins
            reuse.setCoords(it[i].lat, it[i].lon);
            proj.toPixels(reuse, screen);
            final float headX = screen.x;
            final float headY = screen.y - stemLen - coreR;
            final float dxh = e.getX() - headX;
            final float dyh = e.getY() - headY;
            if (dxh * dxh + dyh * dyh <= slop * slop) {
                return i;
            }
        }
        return -1;
    }

    @Override
    public boolean onSingleTapConfirmed(MotionEvent e, MapView mapView) {
        final Listener l = listener;
        if (l == null || !isEnabled()) {
            return false;
        }
        int i = hit(e, mapView);
        if (i < 0) {
            return false; // not ours: let the map handle it
        }
        l.onTap(items[i]);
        return true;
    }

    @Override
    public boolean onLongPress(MotionEvent e, MapView mapView) {
        final Listener l = listener;
        if (l == null || !isEnabled()) {
            return false;
        }
        int i = hit(e, mapView);
        if (i < 0) {
            return false;
        }
        l.onLongPress(items[i]);
        return true;
    }
}
