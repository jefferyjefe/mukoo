package com.mukoo.logger.map;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;

import org.osmdroid.views.MapView;
import org.osmdroid.views.overlay.Overlay;

// Glance-map companion for the suggestion layer: a single small chevron at the
// edge of the circular mini map pointing toward the NEAREST drive target, with
// the distance underneath. The glance map stays live-only (pins would crowd a
// 200dp circle into noise); one arrow gives it direction without clutter —
// "the next place worth driving is that way, 2.3 km".
//
// Fed by the activity: setTargets() once per suggestions refresh (background
// thread ok), setPosition() every live tick. Draws nothing until it has both.
public class TargetArrowOverlay extends Overlay {

    private static final int ACCENT = 0xFF7C4DFF; // same violet as the pins

    private volatile Suggestion[] targets = new Suggestion[0];
    private volatile double posLat = Double.NaN;
    private volatile double posLon = Double.NaN;

    private final float edgeInset;
    private final float arrowLen;
    private final float arrowHalf;
    private final Paint arrowFill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint arrowOutline = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint label = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint labelBg = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Path arrow = new Path();

    public TargetArrowOverlay(float density) {
        this.edgeInset = 26f * density;  // clear of the circular clip + ring
        this.arrowLen = 14f * density;
        this.arrowHalf = 8f * density;

        arrowFill.setStyle(Paint.Style.FILL);
        arrowFill.setColor(ACCENT);
        arrowOutline.setStyle(Paint.Style.STROKE);
        arrowOutline.setStrokeWidth(2f * density);
        arrowOutline.setColor(0xFFFFFFFF);

        label.setColor(0xFFFFFFFF);
        label.setTextAlign(Paint.Align.CENTER);
        label.setFakeBoldText(true);
        label.setTextSize(11f * density);
        labelBg.setStyle(Paint.Style.FILL);
        labelBg.setColor(0xB3202020); // translucent dark chip under the distance
    }

    // Background-thread safe; follow with postInvalidate().
    public void setTargets(java.util.List<Suggestion> suggestions) {
        this.targets = suggestions.toArray(new Suggestion[0]);
    }

    public void setPosition(double lat, double lon) {
        this.posLat = lat;
        this.posLon = lon;
    }

    @Override
    public void draw(Canvas canvas, MapView mapView, boolean shadow) {
        if (shadow) {
            return;
        }
        final Suggestion[] ts = targets;
        final double lat = posLat;
        final double lon = posLon;
        if (ts.length == 0 || Double.isNaN(lat)) {
            return;
        }

        // nearest target as the crow flies (the glance map is orientation, not
        // navigation; the full map + GPX carry the real route).
        Suggestion best = null;
        double bestD = Double.MAX_VALUE;
        for (Suggestion s : ts) {
            double d = SuggestionLayer.distanceM(lat, lon, s.lat, s.lon);
            if (d < bestD) {
                bestD = d;
                best = s;
            }
        }
        if (best == null) {
            return;
        }

        // bearing from here to the target (0 = north, clockwise), then place
        // the chevron on the mini-map edge along that bearing. Screen y grows
        // downward, so north is -y.
        double bearing = Math.atan2(
                Math.toRadians(best.lon - lon)
                        * Math.cos(Math.toRadians((lat + best.lat) / 2.0)),
                Math.toRadians(best.lat - lat));
        final float w = mapView.getWidth();
        final float h = mapView.getHeight();
        final float cx = w / 2f;
        final float cy = h / 2f;
        final float radius = Math.min(w, h) / 2f - edgeInset;
        final float ax = cx + (float) (radius * Math.sin(bearing));
        final float ay = cy - (float) (radius * Math.cos(bearing));

        // chevron pointing outward along the bearing.
        final float dirX = (float) Math.sin(bearing);
        final float dirY = (float) -Math.cos(bearing);
        final float perpX = -dirY;
        final float perpY = dirX;
        arrow.reset();
        arrow.moveTo(ax + dirX * arrowLen, ay + dirY * arrowLen); // point
        arrow.lineTo(ax - dirX * arrowLen * 0.4f + perpX * arrowHalf,
                ay - dirY * arrowLen * 0.4f + perpY * arrowHalf);
        arrow.lineTo(ax - dirX * arrowLen * 0.4f - perpX * arrowHalf,
                ay - dirY * arrowLen * 0.4f - perpY * arrowHalf);
        arrow.close();
        canvas.drawPath(arrow, arrowFill);
        canvas.drawPath(arrow, arrowOutline);

        // distance chip just inside the arrow, toward the centre.
        String text = formatDistance(bestD);
        final float tx = ax - dirX * (arrowLen + 14f);
        final float ty = ay - dirY * (arrowLen + 14f);
        final float halfW = label.measureText(text) / 2f + 8f;
        final float halfH = label.getTextSize() * 0.85f;
        canvas.drawRoundRect(tx - halfW, ty - halfH, tx + halfW, ty + halfH * 0.6f,
                8f, 8f, labelBg);
        canvas.drawText(text, tx, ty, label);
    }

    static String formatDistance(double metres) {
        if (metres < 950) {
            return Math.round(metres / 10.0) * 10 + "m";
        }
        return String.format(java.util.Locale.US, "%.1fkm", metres / 1000.0);
    }
}
