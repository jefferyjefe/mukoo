package com.mukoo.logger.map;

// The one place the RSRP -> colour mapping lives. The live marker, every history
// dot, and any future prediction/uncertainty overlay all colour through here, so
// the scale reads identically across layers.
//
// Typical LTE RSRP buckets (dBm):
//   >= -90            strong   green
//   -90  .. -105      ok       yellow
//   -105 .. -115      weak     orange
//   <  -115           poor     red
//   network_type=none dead     black   (no serving cell -> gaps stand out)
//
// Boundaries resolve by ">=": -105 is yellow, -115 is orange.
public final class RsrpColor {

    private RsrpColor() {
    }

    public static final int NO_SIGNAL = 0xFF000000; // black  — dead zone
    public static final int STRONG    = 0xFF2ECC40; // green  — >= -90
    public static final int OK        = 0xFFFFDC00; // yellow — -90 .. -105
    public static final int WEAK      = 0xFFFF851B; // orange — -105 .. -115
    public static final int POOR      = 0xFFFF4136; // red    — < -115

    // A dead zone (network_type "none", or a null rsrp with no reading) is black:
    // absence of coverage is the primary signal this app maps, so it must be
    // visually obvious, never blended into the weak-signal end of the scale.
    public static int forSample(Double rsrp, String networkType) {
        if ("none".equals(networkType) || rsrp == null) {
            return NO_SIGNAL;
        }
        double v = rsrp;
        if (v >= -90) {
            return STRONG;
        }
        if (v >= -105) {
            return OK;
        }
        if (v >= -115) {
            return WEAK;
        }
        return POOR;
    }
}
