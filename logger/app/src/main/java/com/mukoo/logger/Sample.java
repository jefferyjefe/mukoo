package com.mukoo.logger;

// one measurement. mirrors the ingest api's sample schema, plus session_id which
// travels at the batch level on upload but is handy to keep per-row locally.
// the boxed Double fields are nullable on purpose: a dead zone has no rsrp/rsrq/
// sinr/cell_id, and gps does not always report speed or bearing.
public class Sample {
    public String sampleId;      // client-generated uuid, the server's idempotency key
    public String sessionId;     // one uuid per drive
    public String recordedAt;    // iso-8601 utc, e.g. 2026-07-10T18:04:12.482Z
    // the modem's own timestamp for this reading, iso-8601 utc, or null when it
    // supplied none (dead zone, or a modem that does not report one). rows in a
    // session sharing this value are re-reads of a single measurement.
    public String modemReportedAt;
    public double lat;
    public double lon;
    public String networkType;   // "LTE" | "5G-NR" | "none"
    public Double rsrp;
    public Double rsrq;
    public Double sinr;
    public String cellId;
    public Double speedMps;
    public Double headingDeg;
}
