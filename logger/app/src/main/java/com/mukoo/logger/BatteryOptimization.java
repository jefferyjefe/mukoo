package com.mukoo.logger;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.PowerManager;
import android.provider.Settings;

// Battery-optimization exemption. Aggressive background restriction is the usual
// reason a long unattended drive stops logging: the OS kills the backgrounded
// process. Exempting the app makes the system leave it alone.
public final class BatteryOptimization {

    private BatteryOptimization() {
    }

    public static boolean isExempt(Context c) {
        PowerManager pm = (PowerManager) c.getSystemService(Context.POWER_SERVICE);
        return pm != null && pm.isIgnoringBatteryOptimizations(c.getPackageName());
    }

    // Opens the system "let this app ignore battery optimization?" dialog. Needs
    // REQUEST_IGNORE_BATTERY_OPTIMIZATIONS; if that intent isn't available, falls
    // back to the optimization settings list.
    @SuppressLint("BatteryLife")
    public static void request(Context c) {
        try {
            Intent direct = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + c.getPackageName()));
            direct.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            c.startActivity(direct);
        } catch (Exception e) {
            try {
                Intent list = new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
                list.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                c.startActivity(list);
            } catch (Exception ignored) {
                // no settings surface to open; nothing more we can do.
            }
        }
    }
}
