package com.mukoo.logger.map;

// One active-learning drive target: "go here next to shrink the model's
// uncertainty most". Produced server-side by the model package and delivered as
// GeoJSON; this is the parsed, display-ready value the suggestion layer draws.
// rank is 1-based (1 = highest priority). roadName may be null (unnamed way).
public final class Suggestion {

    public final int rank;
    public final double lat;
    public final double lon;
    public final double stddev;   // kriging 1-sigma uncertainty at the target (dBm)
    public final String roadName; // nullable

    public Suggestion(int rank, double lat, double lon, double stddev, String roadName) {
        this.rank = rank;
        this.lat = lat;
        this.lon = lon;
        this.stddev = stddev;
        this.roadName = roadName;
    }
}
