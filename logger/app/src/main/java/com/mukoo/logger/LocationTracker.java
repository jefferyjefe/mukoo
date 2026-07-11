package com.mukoo.logger;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Looper;

// keeps the most recent gps fix while a drive is active. we hold the latest fix
// and let the sampler read it on its own cadence, rather than emitting a sample
// per gps callback, so the sample rate is driven by us, not the gps chip.
public class LocationTracker {

    private final Context context;
    private final LocationManager lm;
    private volatile Location last;

    private final LocationListener listener = new LocationListener() {
        @Override
        public void onLocationChanged(Location location) {
            last = location;
        }

        // no-ops, but implemented for compatibility across api levels.
        @Override
        public void onProviderEnabled(String provider) {
        }

        @Override
        public void onProviderDisabled(String provider) {
        }
    };

    public LocationTracker(Context context) {
        this.context = context.getApplicationContext();
        this.lm = (LocationManager) this.context.getSystemService(Context.LOCATION_SERVICE);
    }

    public void start() {
        if (lm == null || !hasPermission()) {
            return;
        }
        // seed with a last known fix so the first sample is not blank if one
        // already exists.
        try {
            Location known = lm.getLastKnownLocation(LocationManager.GPS_PROVIDER);
            if (known == null) {
                known = lm.getLastKnownLocation(LocationManager.NETWORK_PROVIDER);
            }
            if (known != null) {
                last = known;
            }
        } catch (SecurityException ignored) {
        }

        try {
            lm.requestLocationUpdates(
                LocationManager.GPS_PROVIDER, 1000L, 0f, listener, Looper.getMainLooper());
            // network provider as a backup during a cold start / heavy cover.
            if (lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                lm.requestLocationUpdates(
                    LocationManager.NETWORK_PROVIDER, 1000L, 0f, listener, Looper.getMainLooper());
            }
        } catch (SecurityException ignored) {
        }
    }

    public void stop() {
        if (lm != null) {
            lm.removeUpdates(listener);
        }
    }

    public Location getLast() {
        return last;
    }

    private boolean hasPermission() {
        return context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }
}
