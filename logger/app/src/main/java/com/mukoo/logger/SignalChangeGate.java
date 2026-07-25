package com.mukoo.logger;

import java.util.Objects;

// decides whether a fresh modem read is worth storing.
//
// the sampler ticks every ~3s, but the modem refreshes its signal report far more
// slowly — measured on the existing dataset, ~86% of stored rows repeated the
// previous (rsrp, rsrq, sinr) triple exactly. those rows are not independent
// observations: they are one measurement fetched several times, and feeding them
// to a variogram overstates the sample size and distorts the short-lag structure
// (kriging on the deduplicated data cross-validates materially better).
//
// so the gate keeps a reading only when something about it actually changed. it
// is deliberately a plain object with no Android dependencies: the sampler is a
// foreground Service, and this is the part with the logic worth testing.
//
// what counts as a change, and why:
//   - any of rsrp / rsrq / sinr differs (null <-> value included). the signal
//     itself moved, which is the whole point.
//   - network_type differs. LTE -> none is entering a dead zone: the most
//     important transition this project records, never thin it away.
//   - cell_id differs. a handover means the modem measured a different cell, so
//     identical numbers are a coincidence, not a re-read.
//   - nothing changed but UNCHANGED_KEEPALIVE_MS has passed. a floor, so a long
//     stretch of genuinely steady signal still leaves a spatial trace and a
//     stalled logger stays distinguishable from a quiet one.
//
// note the modem's own timestamp is deliberately NOT part of this decision. it
// rides along on the sample so re-reads are identifiable at ingestion; letting it
// force a write here would reinstate exactly the rows this gate exists to drop,
// since the framework can restamp a latched value on every poll.
public class SignalChangeGate {

    // Upper bound on the gap between stored samples when the signal is steady,
    // so a drive through unchanging coverage still leaves a spatial trace.
    //
    // Chosen by sweeping this value over the first 3,130 real samples and
    // measuring both the reduction and the distance between kept samples:
    //
    //     keepalive   kept   reduction   gap p95
    //           off    427        7.3x     1471 m
    //           15s    918        3.4x      421 m
    //           30s    502        6.2x      924 m
    //           60s    463        6.8x     1297 m
    //           90s    448        7.0x     1532 m
    //
    // 30s is the elbow: it halves the p95 gap against 60s for 8% more rows,
    // whereas 60 -> 90s buys almost no further reduction for another 235 m of
    // gap. It also matches DriveSessionService.STATIONARY_KEEP_INTERVAL_MS, so
    // the app has one "insist on a sample this often" cadence rather than two.
    static final long UNCHANGED_KEEPALIVE_MS = 30_000L;

    private boolean primed = false;
    private String lastNetworkType;
    private String lastCellId;
    private Double lastRsrp;
    private Double lastRsrq;
    private Double lastSinr;
    private long lastWriteElapsedMs;

    // true when this reading should be stored. records it as the new baseline in
    // that case, so callers cannot forget to — one call, one decision.
    // nowElapsedMs must come from SystemClock.elapsedRealtime(): it has to be
    // immune to wall-clock jumps mid-drive.
    public boolean admit(SignalReader.Reading r, long nowElapsedMs) {
        if (shouldWrite(r, nowElapsedMs)) {
            lastNetworkType = r.networkType;
            lastCellId = r.cellId;
            lastRsrp = r.rsrp;
            lastRsrq = r.rsrq;
            lastSinr = r.sinr;
            lastWriteElapsedMs = nowElapsedMs;
            primed = true;
            return true;
        }
        return false;
    }

    private boolean shouldWrite(SignalReader.Reading r, long nowElapsedMs) {
        if (!primed) {
            return true;  // first reading of the session is always a change.
        }
        if (!Objects.equals(lastRsrp, r.rsrp)
                || !Objects.equals(lastRsrq, r.rsrq)
                || !Objects.equals(lastSinr, r.sinr)) {
            return true;
        }
        if (!Objects.equals(lastNetworkType, r.networkType)
                || !Objects.equals(lastCellId, r.cellId)) {
            return true;
        }
        // Unsigned subtraction is safe here: elapsedRealtime is monotonic.
        return (nowElapsedMs - lastWriteElapsedMs) >= UNCHANGED_KEEPALIVE_MS;
    }

    // reset between drives so a new session never inherits the previous one's
    // baseline and drops its own first reading.
    public void reset() {
        primed = false;
        lastNetworkType = null;
        lastCellId = null;
        lastRsrp = null;
        lastRsrq = null;
        lastSinr = null;
        lastWriteElapsedMs = 0L;
    }
}
