package com.mukoo.logger.map;

// One active-learning drive target: "go here next to shrink the model's
// uncertainty most". Produced server-side by the model package and delivered as
// GeoJSON; this is the parsed, display-ready value the suggestion layer draws.
// rank is 1-based (1 = highest priority). roadName may be null (unnamed way).
// visitOrder is the server's suggested driving order (2-opt tour); 0 = unknown.
public final class Suggestion {

    public final int rank;
    public final double lat;
    public final double lon;
    public final double stddev;   // kriging 1-sigma uncertainty at the target (dBm)
    public final String roadName; // nullable
    public final int visitOrder;  // 1-based driving order, 0 when absent

    public Suggestion(int rank, double lat, double lon, double stddev, String roadName,
                      int visitOrder) {
        this.rank = rank;
        this.lat = lat;
        this.lon = lon;
        this.stddev = stddev;
        this.roadName = roadName;
        this.visitOrder = visitOrder;
    }
}
