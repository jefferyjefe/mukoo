package com.mukoo.logger.map;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Point;

import org.osmdroid.util.GeoPoint;
import org.osmdroid.views.MapView;
import org.osmdroid.views.overlay.Overlay;

// Draws the single "you are here" marker: a fat dot coloured by the current RSRP
// (same scale as history), sitting inside a white halo with a dark rim so it
// stays legible over green history dots, grey tiles, or a dead-zone black patch.
// Nothing until the first fix arrives.
public class LiveOverlay extends Overlay {

    private volatile GeoPoint pos;
    private volatile int color = RsrpColor.NO_SIGNAL;

    private final float coreR;
    private final float haloR;
    private final Paint halo = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint rim = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Point screen = new Point();

    public LiveOverlay(float density) {
        this.coreR = 8f * density;
        this.haloR = coreR + 3f * density;
        halo.setStyle(Paint.Style.FILL);
        halo.setColor(0xFFFFFFFF);
        fill.setStyle(Paint.Style.FILL);
        rim.setStyle(Paint.Style.STROKE);
        rim.setStrokeWidth(2f * density);
        rim.setColor(0xFF202020);
    }

    // Move the marker and recolour it. Safe from any thread; follow with
    // MapView.postInvalidate() (or an invalidate on the UI thread).
    public void update(GeoPoint p, int color) {
        this.pos = p;
        this.color = color;
    }

    public boolean hasFix() {
        return pos != null;
    }

    @Override
    public void draw(Canvas canvas, MapView mapView, boolean shadow) {
        if (shadow) {
            return;
        }
        final GeoPoint p = pos;
        if (p == null) {
            return;
        }
        mapView.getProjection().toPixels(p, screen);
        fill.setColor(color);
        canvas.drawCircle(screen.x, screen.y, haloR, halo);
        canvas.drawCircle(screen.x, screen.y, coreR, fill);
        canvas.drawCircle(screen.x, screen.y, coreR, rim);
    }
}
