package com.mukoo.logger.map;

import android.location.Location;
import android.widget.TextView;

import com.mukoo.logger.SignalReader;

// Formats a live reading into the always-visible two-line metrics panel. Shared
// so the glance map and the full map show identical text and identical RSRP
// colouring — one place, one scale (RsrpColor). Both panels sit on a dark
// background, so the bucket colours stay legible.
public final class LiveMetrics {

    private LiveMetrics() {
    }

    // main: big line — network + RSRP, coloured by the RSRP bucket (the at-a-
    // glance quality read). sub: RSRQ / SINR / cell id, or GPS status.
    public static void render(TextView main, TextView sub, Location loc, SignalReader.Reading r) {
        int color = RsrpColor.forSample(r.rsrp, r.networkType);
        String mainText = "none".equals(r.networkType)
                ? "NO SIGNAL"
                : r.networkType + "  " + fmt(r.rsrp) + " dBm";
        main.setTextColor(panelTextColor(color));
        main.setText(mainText);

        if (loc == null) {
            sub.setText("waiting for GPS…");
        } else {
            StringBuilder b = new StringBuilder();
            b.append("RSRQ ").append(fmt(r.rsrq)).append("   SINR ").append(fmt(r.sinr));
            if (r.cellId != null) {
                b.append("   CID ").append(r.cellId);
            }
            sub.setText(b.toString());
        }
    }

    // black (dead zone) would vanish on the dark panel, so alert in red there;
    // every other bucket colour is legible as-is.
    private static int panelTextColor(int rsrpColor) {
        return rsrpColor == RsrpColor.NO_SIGNAL ? 0xFFFF6E6E : rsrpColor;
    }

    private static String fmt(Double v) {
        return v == null ? "--" : Long.toString(Math.round(v));
    }
}
