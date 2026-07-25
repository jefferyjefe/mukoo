package com.mukoo.logger;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

// The gate is the whole point of the change-detection work, and it is a plain
// object precisely so it can be tested without an emulator.
public class SignalChangeGateTest {

    private static SignalReader.Reading reading(
            String networkType, Double rsrp, Double rsrq, Double sinr, String cellId) {
        SignalReader.Reading r = new SignalReader.Reading();
        r.networkType = networkType;
        r.rsrp = rsrp;
        r.rsrq = rsrq;
        r.sinr = sinr;
        r.cellId = cellId;
        return r;
    }

    private static SignalReader.Reading lte(double rsrp) {
        return reading("LTE", rsrp, -10.0, 5.0, "cell-1");
    }

    @Test
    public void firstReadingIsAlwaysAdmitted() {
        SignalChangeGate gate = new SignalChangeGate();
        assertTrue(gate.admit(lte(-100.0), 0L));
    }

    @Test
    public void identicalReadingIsSuppressed() {
        SignalChangeGate gate = new SignalChangeGate();
        assertTrue(gate.admit(lte(-100.0), 0L));
        assertFalse(gate.admit(lte(-100.0), 3_000L));
        assertFalse(gate.admit(lte(-100.0), 6_000L));
    }

    @Test
    public void anyChangedMetricIsAdmitted() {
        SignalChangeGate gate = new SignalChangeGate();
        gate.admit(reading("LTE", -100.0, -10.0, 5.0, "c1"), 0L);
        // rsrp moves
        assertTrue(gate.admit(reading("LTE", -101.0, -10.0, 5.0, "c1"), 3_000L));
        // rsrq moves
        assertTrue(gate.admit(reading("LTE", -101.0, -11.0, 5.0, "c1"), 6_000L));
        // sinr moves
        assertTrue(gate.admit(reading("LTE", -101.0, -11.0, 6.0, "c1"), 9_000L));
    }

    @Test
    public void nullToValueTransitionIsAChange() {
        SignalChangeGate gate = new SignalChangeGate();
        gate.admit(reading("LTE", -100.0, -10.0, null, "c1"), 0L);
        assertTrue(gate.admit(reading("LTE", -100.0, -10.0, 5.0, "c1"), 3_000L));
        // ...and back again.
        assertTrue(gate.admit(reading("LTE", -100.0, -10.0, null, "c1"), 6_000L));
    }

    @Test
    public void enteringADeadZoneIsAlwaysAdmitted() {
        // LTE -> none is the transition this project exists to record; it must
        // never be thinned away, even though every metric merely goes null.
        SignalChangeGate gate = new SignalChangeGate();
        gate.admit(lte(-120.0), 0L);
        assertTrue(gate.admit(reading("none", null, null, null, null), 3_000L));
        // Consecutive dead-zone reads are still re-reads, so they suppress.
        assertFalse(gate.admit(reading("none", null, null, null, null), 6_000L));
        // Regaining coverage is a change again.
        assertTrue(gate.admit(lte(-118.0), 9_000L));
    }

    @Test
    public void handoverToAnotherCellIsAdmittedEvenWithIdenticalMetrics() {
        // Same numbers from a different cell is a coincidence, not a re-read.
        SignalChangeGate gate = new SignalChangeGate();
        gate.admit(reading("LTE", -100.0, -10.0, 5.0, "cell-a"), 0L);
        assertTrue(gate.admit(reading("LTE", -100.0, -10.0, 5.0, "cell-b"), 3_000L));
    }

    @Test
    public void steadySignalStillWritesOncePerKeepaliveWindow() {
        SignalChangeGate gate = new SignalChangeGate();
        assertTrue(gate.admit(lte(-100.0), 0L));
        // Just short of the window: still suppressed.
        assertFalse(gate.admit(lte(-100.0), SignalChangeGate.UNCHANGED_KEEPALIVE_MS - 1));
        // At the window: one sample gets through so a steady drive leaves a trace.
        assertTrue(gate.admit(lte(-100.0), SignalChangeGate.UNCHANGED_KEEPALIVE_MS));
        // The window restarts from that write.
        assertFalse(gate.admit(lte(-100.0), SignalChangeGate.UNCHANGED_KEEPALIVE_MS + 1));
    }

    @Test
    public void resetDropsTheBaselineSoANewDriveKeepsItsFirstReading() {
        SignalChangeGate gate = new SignalChangeGate();
        gate.admit(lte(-100.0), 0L);
        assertFalse(gate.admit(lte(-100.0), 3_000L));
        gate.reset();
        assertTrue(gate.admit(lte(-100.0), 6_000L));
    }

    @Test
    public void modemTimestampDoesNotByItselfForceAWrite() {
        // The framework can restamp a latched value on every poll, so letting the
        // timestamp drive the decision would reinstate the very rows being cut.
        SignalChangeGate gate = new SignalChangeGate();
        SignalReader.Reading first = lte(-100.0);
        first.modemReportedAtMs = 1_000L;
        assertTrue(gate.admit(first, 0L));

        SignalReader.Reading restamped = lte(-100.0);
        restamped.modemReportedAtMs = 4_000L;  // advanced, values identical
        assertFalse(gate.admit(restamped, 3_000L));
    }

    @Test
    public void realisticStationaryStretchCollapsesToKeepaliveRate() {
        // 5 minutes parked at a 3s tick = 100 ticks. Without the gate that is 100
        // rows; with it, only the keepalive samples survive.
        SignalChangeGate gate = new SignalChangeGate();
        int written = 0;
        for (long t = 0; t < 300_000L; t += 3_000L) {
            if (gate.admit(lte(-100.0), t)) {
                written++;
            }
        }
        assertEquals(300_000L / SignalChangeGate.UNCHANGED_KEEPALIVE_MS, written);
    }

    @Test
    public void movingStretchWithChangingSignalKeepsEveryDistinctReading() {
        // The other half of the contract: real change must survive the gate.
        SignalChangeGate gate = new SignalChangeGate();
        int written = 0;
        for (int i = 0; i < 20; i++) {
            if (gate.admit(lte(-100.0 - i), i * 3_000L)) {
                written++;
            }
        }
        assertEquals(20, written);
    }
}
