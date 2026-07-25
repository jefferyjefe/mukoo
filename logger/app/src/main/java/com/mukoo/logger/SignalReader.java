package com.mukoo.logger;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.SystemClock;
import android.telephony.CellIdentityLte;
import android.telephony.CellIdentityNr;
import android.telephony.CellInfo;
import android.telephony.CellInfoLte;
import android.telephony.CellInfoNr;
import android.telephony.CellSignalStrengthLte;
import android.telephony.CellSignalStrengthNr;
import android.telephony.TelephonyManager;

import java.util.List;

// reads the serving cell's signal metrics off the modem via TelephonyManager.
// verizon retired 3g in 2022, so in the field this is lte, 5g-nr, or nothing;
// the backend enum is exactly those three. anything that is neither lte nor nr
// (or no registered cell at all) we record as a dead zone: network_type "none"
// with null metrics. absence of coverage is the primary signal we are mapping,
// so it is data, not an error to skip.
public class SignalReader {

    // Beyond this, a CellInfo timestamp is treated as uninterpretable rather than
    // trusted. Cell state turns over in seconds; a reading the modem claims is
    // ten minutes old is a clock we should not reason from.
    static final long MAX_MODEM_TIMESTAMP_AGE_MS = 10 * 60 * 1000L;

    // just the radio-side fields; the sampler adds gps + the ids on top.
    public static class Reading {
        public String networkType = "none";
        public Double rsrp;
        public Double rsrq;
        public Double sinr;
        public String cellId;
        // wall-clock millis the modem stamped this reading, or null if it gave
        // none. this is the modem's clock, not ours: two reads that return the
        // same value AND the same modemReportedAtMs are one measurement fetched
        // twice, which is what makes latched re-reads identifiable server-side
        // instead of merely guessable from equal values.
        public Long modemReportedAtMs;
    }

    private final Context context;
    private final TelephonyManager tm;

    public SignalReader(Context context) {
        this.context = context.getApplicationContext();
        this.tm = (TelephonyManager) this.context.getSystemService(Context.TELEPHONY_SERVICE);
    }

    public Reading read() {
        Reading r = new Reading();

        // getAllCellInfo requires fine location. without it (or with no modem) we
        // honestly report a dead zone instead of guessing.
        if (tm == null || context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            return r;
        }

        List<CellInfo> infos;
        try {
            infos = tm.getAllCellInfo();
        } catch (SecurityException e) {
            return r;
        }
        if (infos == null) {
            return r;
        }

        // pick the registered serving cell. if both nr and lte register (nsa 5g),
        // prefer nr for the headline network type.
        CellInfoNr nr = null;
        CellInfoLte lte = null;
        for (CellInfo info : infos) {
            if (!info.isRegistered()) {
                continue;
            }
            if (info instanceof CellInfoNr) {
                nr = (CellInfoNr) info;
            } else if (info instanceof CellInfoLte) {
                lte = (CellInfoLte) info;
            }
        }

        if (nr != null) {
            fillNr(r, nr);
            r.modemReportedAtMs = wallClockOf(nr);
        } else if (lte != null) {
            fillLte(r, lte);
            r.modemReportedAtMs = wallClockOf(lte);
        }
        // otherwise r stays the default "none" dead-zone reading. no serving cell
        // means no CellInfo to carry a timestamp, so modemReportedAtMs stays null.
        return r;
    }

    // CellInfo timestamps are on the boot-relative clock (SystemClock
    // .elapsedRealtime), which is meaningless off-device. Convert to wall clock
    // via the reading's age so the server gets an absolute instant comparable to
    // recorded_at. Age, not the raw value, is the invariant: elapsedRealtime is
    // immune to wall-clock jumps (NTP, timezone), so deriving from it survives
    // a clock correction mid-drive.
    private static Long wallClockOf(CellInfo info) {
        long ageMs = SystemClock.elapsedRealtime() - info.getTimestampMillis();
        // Sanity-guard the arithmetic rather than trusting the modem. A negative
        // age (timestamp in the future) or an implausibly old one means we cannot
        // interpret it, and a wrong timestamp is worse than none: it would make
        // distinct measurements look like one re-read.
        if (ageMs < 0L || ageMs > MAX_MODEM_TIMESTAMP_AGE_MS) {
            return null;
        }
        return System.currentTimeMillis() - ageMs;
    }

    private void fillNr(Reading r, CellInfoNr info) {
        r.networkType = "5G-NR";
        CellSignalStrengthNr ss = (CellSignalStrengthNr) info.getCellSignalStrength();
        r.rsrp = clean(ss.getSsRsrp());
        r.rsrq = clean(ss.getSsRsrq());
        r.sinr = clean(ss.getSsSinr());
        CellIdentityNr id = (CellIdentityNr) info.getCellIdentity();
        long nci = id.getNci();
        if (nci != CellInfo.UNAVAILABLE_LONG) {
            r.cellId = Long.toString(nci);
        }
    }

    private void fillLte(Reading r, CellInfoLte info) {
        r.networkType = "LTE";
        CellSignalStrengthLte ss = info.getCellSignalStrength();
        r.rsrp = clean(ss.getRsrp());
        r.rsrq = clean(ss.getRsrq());
        r.sinr = clean(ss.getRssnr());
        CellIdentityLte id = info.getCellIdentity();
        int ci = id.getCi();
        if (ci != CellInfo.UNAVAILABLE) {
            r.cellId = Integer.toString(ci);
        }
    }

    // the telephony api uses Integer.MAX_VALUE (CellInfo.UNAVAILABLE) as its
    // "no reading" sentinel; turn that into a real null so a dead metric never
    // masquerades as a huge dBm value.
    private static Double clean(int v) {
        return v == CellInfo.UNAVAILABLE ? null : (double) v;
    }
}
