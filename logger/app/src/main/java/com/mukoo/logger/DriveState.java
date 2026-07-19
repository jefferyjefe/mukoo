package com.mukoo.logger;

import android.os.SystemClock;

// A tiny in-memory view of the running drive, published by DriveSessionService
// and read by the main screen's health line. The service and the UI share one
// process, so this needs no IPC.
//
// This is display state, not the source of truth: whether a drive is active and
// which session id it belongs to are persisted to disk by DriveSessionService so
// they survive an OS restart. This just gives the health line a cheap, current
// read of "is logging alive and when did the last sample land".
public final class DriveState {

    private DriveState() {
    }

    private static volatile boolean driving = false;
    // SystemClock.elapsedRealtime() of the last successful insert; 0 == none yet
    // this process. elapsedRealtime is monotonic and unaffected by clock changes.
    private static volatile long lastSampleElapsedMs = 0L;

    // service calls this when a drive starts or resumes.
    public static void onDriveStarted() {
        driving = true;
        lastSampleElapsedMs = 0L; // a fresh drive is honestly "no sample yet"
    }

    // service calls this on an explicit stop.
    public static void onDriveStopped() {
        driving = false;
    }

    // service calls this after every sample it writes to the buffer.
    public static void onSampleRecorded() {
        lastSampleElapsedMs = SystemClock.elapsedRealtime();
    }

    public static boolean isDriving() {
        return driving;
    }

    public static long lastSampleElapsedMs() {
        return lastSampleElapsedMs;
    }
}
