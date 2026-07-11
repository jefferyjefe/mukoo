package com.mukoo.logger;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
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

    // just the radio-side fields; the sampler adds gps + the ids on top.
    public static class Reading {
        public String networkType = "none";
        public Double rsrp;
        public Double rsrq;
        public Double sinr;
        public String cellId;
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
        } else if (lte != null) {
            fillLte(r, lte);
        }
        // otherwise r stays the default "none" dead-zone reading.
        return r;
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
