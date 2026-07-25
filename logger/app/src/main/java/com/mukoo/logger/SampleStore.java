package com.mukoo.logger;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.util.ArrayList;
import java.util.List;

// local store-and-forward buffer. every sample lands here first (even with zero
// connectivity) and is only flagged uploaded once the ingest api has acked it.
public class SampleStore extends SQLiteOpenHelper {

    private static final String DB_NAME = "mukoo.db";
    // v2 adds modem_reported_at. bumping this runs onUpgrade on an existing
    // install rather than losing the buffer: unsent rows from a v1 database are
    // mid-drive data, and this store is also the recovery source of truth for the
    // server (it keeps every sample forever, uploaded or not).
    private static final int DB_VERSION = 2;
    private static final String TABLE = "samples";

    public SampleStore(Context context) {
        super(context.getApplicationContext(), DB_NAME, null, DB_VERSION);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL(
            "CREATE TABLE " + TABLE + " (" +
            "_id INTEGER PRIMARY KEY AUTOINCREMENT, " +
            "sample_id TEXT NOT NULL UNIQUE, " +
            "session_id TEXT NOT NULL, " +
            "recorded_at TEXT NOT NULL, " +
            "modem_reported_at TEXT, " +
            "lat REAL NOT NULL, " +
            "lon REAL NOT NULL, " +
            "network_type TEXT NOT NULL, " +
            "rsrp REAL, rsrq REAL, sinr REAL, " +
            "cell_id TEXT, speed_mps REAL, heading_deg REAL, " +
            "uploaded INTEGER NOT NULL DEFAULT 0)");
        // the uploader's hot path is "find unsent rows", so index that.
        db.execSQL("CREATE INDEX ix_samples_unsent ON " + TABLE + " (uploaded, session_id, _id)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        // additive only, and never destructive: rows already here may be unsent
        // drive data, and this database is the server's recovery source.
        if (oldVersion < 2) {
            // nullable with no default: rows recorded before the column existed
            // genuinely have no modem timestamp, and NULL says that honestly.
            db.execSQL("ALTER TABLE " + TABLE + " ADD COLUMN modem_reported_at TEXT");
        }
    }

    public void insert(Sample s) {
        ContentValues v = new ContentValues();
        v.put("sample_id", s.sampleId);
        v.put("session_id", s.sessionId);
        v.put("recorded_at", s.recordedAt);
        if (s.modemReportedAt != null) {
            v.put("modem_reported_at", s.modemReportedAt);
        } else {
            v.putNull("modem_reported_at");
        }
        v.put("lat", s.lat);
        v.put("lon", s.lon);
        v.put("network_type", s.networkType);
        putNullable(v, "rsrp", s.rsrp);
        putNullable(v, "rsrq", s.rsrq);
        putNullable(v, "sinr", s.sinr);
        if (s.cellId != null) {
            v.put("cell_id", s.cellId);
        } else {
            v.putNull("cell_id");
        }
        putNullable(v, "speed_mps", s.speedMps);
        putNullable(v, "heading_deg", s.headingDeg);
        v.put("uploaded", 0);
        // CONFLICT_IGNORE: a repeated sample_id (should not happen) must never
        // crash the sampler mid-drive.
        getWritableDatabase().insertWithOnConflict(TABLE, null, v, SQLiteDatabase.CONFLICT_IGNORE);
    }

    // a lightweight row for the map's history layer: just what it takes to plot a
    // dot. keeping it separate from Sample avoids dragging upload bookkeeping into
    // the render path.
    public static final class GeoSample {
        public final double lat;
        public final double lon;
        public final Double rsrp;        // null in a dead zone
        public final String networkType; // "LTE" | "5G-NR" | "none"

        GeoSample(double lat, double lon, Double rsrp, String networkType) {
            this.lat = lat;
            this.lon = lon;
            this.rsrp = rsrp;
            this.networkType = networkType;
        }
    }

    // every logged sample's position + rsrp + network type, oldest first. this is
    // the whole history layer's data source: local SQLite, so the map draws driven
    // roads with zero connectivity. uploaded or not is irrelevant here — a sample
    // is history the moment it is recorded. call off the UI thread.
    public List<GeoSample> allSamplesForMap() {
        Cursor c = getReadableDatabase().rawQuery(
            "SELECT lat, lon, rsrp, network_type FROM " + TABLE + " ORDER BY _id", null);
        List<GeoSample> out = new ArrayList<>(c.getCount());
        try {
            while (c.moveToNext()) {
                out.add(new GeoSample(
                    c.getDouble(0),
                    c.getDouble(1),
                    c.isNull(2) ? null : c.getDouble(2),
                    c.getString(3)));
            }
        } finally {
            c.close();
        }
        return out;
    }

    public int countAll() {
        return count("SELECT COUNT(*) FROM " + TABLE);
    }

    public int countUploaded() {
        return count("SELECT COUNT(*) FROM " + TABLE + " WHERE uploaded = 1");
    }

    public int countUnsent() {
        return count("SELECT COUNT(*) FROM " + TABLE + " WHERE uploaded = 0");
    }

    // oldest unsent session, or null when the buffer is fully flushed. batches
    // are single-session because the ingest schema carries session_id once at the
    // batch level.
    public String nextUnsentSession() {
        Cursor c = getReadableDatabase().rawQuery(
            "SELECT session_id FROM " + TABLE + " WHERE uploaded = 0 ORDER BY _id LIMIT 1", null);
        try {
            return c.moveToFirst() ? c.getString(0) : null;
        } finally {
            c.close();
        }
    }

    public List<Sample> unsentForSession(String sessionId, int limit) {
        Cursor c = getReadableDatabase().rawQuery(
            "SELECT sample_id, recorded_at, lat, lon, network_type, rsrp, rsrq, sinr, " +
            "cell_id, speed_mps, heading_deg, modem_reported_at FROM " + TABLE +
            " WHERE uploaded = 0 AND session_id = ? ORDER BY _id LIMIT " + limit,
            new String[]{sessionId});
        List<Sample> out = new ArrayList<>();
        try {
            while (c.moveToNext()) {
                Sample s = new Sample();
                s.sessionId = sessionId;
                s.sampleId = c.getString(0);
                s.recordedAt = c.getString(1);
                s.lat = c.getDouble(2);
                s.lon = c.getDouble(3);
                s.networkType = c.getString(4);
                s.rsrp = nullableDouble(c, 5);
                s.rsrq = nullableDouble(c, 6);
                s.sinr = nullableDouble(c, 7);
                s.cellId = c.isNull(8) ? null : c.getString(8);
                s.speedMps = nullableDouble(c, 9);
                s.headingDeg = nullableDouble(c, 10);
                // null for rows written by a pre-v2 build; those still upload.
                s.modemReportedAt = c.isNull(11) ? null : c.getString(11);
                out.add(s);
            }
        } finally {
            c.close();
        }
        return out;
    }

    public void markUploaded(List<String> sampleIds) {
        if (sampleIds.isEmpty()) {
            return;
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            for (String id : sampleIds) {
                ContentValues v = new ContentValues();
                v.put("uploaded", 1);
                db.update(TABLE, v, "sample_id = ?", new String[]{id});
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    private int count(String sql) {
        Cursor c = getReadableDatabase().rawQuery(sql, null);
        try {
            c.moveToFirst();
            return c.getInt(0);
        } finally {
            c.close();
        }
    }

    private static void putNullable(ContentValues v, String key, Double value) {
        if (value == null) {
            v.putNull(key);
        } else {
            v.put(key, value);
        }
    }

    private static Double nullableDouble(Cursor c, int col) {
        return c.isNull(col) ? null : c.getDouble(col);
    }
}
