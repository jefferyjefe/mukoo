package com.mukoo.logger;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;

import java.util.UUID;

// minimal field ui: one start/stop button plus live counts of samples logged vs
// uploaded. the real work is in DriveSessionService; the activity just toggles it
// and polls the local db for the two numbers. this is a field tool, not a
// product.
public class MainActivity extends Activity {

    private static final int REQ_PERMS = 100;

    // requested up front. POST_NOTIFICATIONS is nice-to-have (the fgs notice);
    // the rest are essential to logging.
    private static final String[] REQUEST_PERMS = new String[]{
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
            Manifest.permission.READ_PHONE_STATE,
            Manifest.permission.POST_NOTIFICATIONS,
    };

    private Button toggle;
    private TextView loggedView;
    private TextView uploadedView;
    private TextView statusView;

    private SampleStore store;
    private boolean driving = false;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable refresh = new Runnable() {
        @Override
        public void run() {
            updateCounts();
            ui.postDelayed(this, 1000L);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        store = new SampleStore(this);

        toggle = findViewById(R.id.toggle);
        loggedView = findViewById(R.id.logged);
        uploadedView = findViewById(R.id.uploaded);
        statusView = findViewById(R.id.status);

        toggle.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (driving) {
                    stopDrive();
                } else {
                    startDrive();
                }
            }
        });

        // the map is a viewer — it works whether or not a drive is recording, so
        // it opens independently of the start/stop toggle.
        findViewById(R.id.openMap).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startActivity(new Intent(MainActivity.this, MapActivity.class));
            }
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        ui.post(refresh);
    }

    @Override
    protected void onPause() {
        super.onPause();
        ui.removeCallbacks(refresh);
    }

    private void startDrive() {
        if (!hasEssentialPermissions()) {
            requestPermissions(REQUEST_PERMS, REQ_PERMS);
            return;
        }
        String sessionId = UUID.randomUUID().toString();
        Intent i = new Intent(this, DriveSessionService.class);
        i.putExtra(DriveSessionService.EXTRA_SESSION_ID, sessionId);
        startForegroundService(i);
        driving = true;
        toggle.setText("Stop drive");
        statusView.setText("Recording session " + sessionId.substring(0, 8));
    }

    private void stopDrive() {
        stopService(new Intent(this, DriveSessionService.class));
        driving = false;
        toggle.setText("Start drive");
        statusView.setText("Stopped");
    }

    private void updateCounts() {
        loggedView.setText("Logged: " + store.countAll());
        uploadedView.setText("Uploaded: " + store.countUploaded());
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQ_PERMS) {
            return;
        }
        // location + phone state are the ones logging actually needs. if we have
        // them, go; a denied notification permission does not block anything.
        if (hasEssentialPermissions()) {
            startDrive();
        } else {
            statusView.setText("Location and phone permissions are required to log");
        }
    }

    // notifications are intentionally not "essential" here, so denying them never
    // traps startDrive in a re-request loop.
    private boolean hasEssentialPermissions() {
        return granted(Manifest.permission.ACCESS_FINE_LOCATION)
                && granted(Manifest.permission.READ_PHONE_STATE);
    }

    private boolean granted(String perm) {
        return checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED;
    }
}
