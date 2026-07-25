package com.mukoo.logger;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;

import java.io.File;
import java.util.List;

// The v1 -> v2 upgrade runs against a real phone database holding unsent drive
// data, and this store is also the server's recovery source of truth — so the
// migration must add the column WITHOUT touching a single existing row. That is
// exactly the kind of thing worth proving before shipping rather than after.
//
// Robolectric gives us the genuine Android SQLite here, so onUpgrade executes
// the same way it will on the Pixel.
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class SampleStoreUpgradeTest {

    private static final String DB_NAME = "mukoo.db";

    // The v1 schema, verbatim from before modem_reported_at existed.
    private static void createV1(SQLiteDatabase db) {
        db.execSQL(
            "CREATE TABLE samples ("
            + "_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            + "sample_id TEXT NOT NULL UNIQUE, "
            + "session_id TEXT NOT NULL, "
            + "recorded_at TEXT NOT NULL, "
            + "lat REAL NOT NULL, "
            + "lon REAL NOT NULL, "
            + "network_type TEXT NOT NULL, "
            + "rsrp REAL, rsrq REAL, sinr REAL, "
            + "cell_id TEXT, speed_mps REAL, heading_deg REAL, "
            + "uploaded INTEGER NOT NULL DEFAULT 0)");
        db.execSQL("CREATE INDEX ix_samples_unsent ON samples (uploaded, session_id, _id)");
        db.setVersion(1);
    }

    private static void insertV1Row(SQLiteDatabase db, String sampleId, String session,
                                    double rsrp, int uploaded) {
        ContentValues v = new ContentValues();
        v.put("sample_id", sampleId);
        v.put("session_id", session);
        v.put("recorded_at", "2026-07-20T12:00:00Z");
        v.put("lat", 32.40);
        v.put("lon", -81.75);
        v.put("network_type", "LTE");
        v.put("rsrp", rsrp);
        v.put("rsrq", -10.0);
        v.put("sinr", 5.0);
        v.put("cell_id", "cell-1");
        v.put("uploaded", uploaded);
        db.insert("samples", null, v);
    }

    /** Build a v1 database on disk where SampleStore will later open it. */
    private File seedV1Database(Context ctx) {
        File path = ctx.getDatabasePath(DB_NAME);
        path.getParentFile().mkdirs();
        SQLiteDatabase db = SQLiteDatabase.openOrCreateDatabase(path, null);
        createV1(db);
        insertV1Row(db, "s-uploaded", "session-old", -100.0, 1);
        insertV1Row(db, "s-unsent-1", "session-live", -101.0, 0);
        insertV1Row(db, "s-unsent-2", "session-live", -102.0, 0);
        db.close();
        return path;
    }

    @Test
    public void upgradeAddsTheColumnAndKeepsEveryExistingRow() {
        Context ctx = RuntimeEnvironment.getApplication();
        seedV1Database(ctx);

        SampleStore store = new SampleStore(ctx);
        SQLiteDatabase db = store.getReadableDatabase();  // triggers onUpgrade

        assertEquals(2, db.getVersion());

        Cursor c = db.rawQuery("PRAGMA table_info(samples)", null);
        boolean hasColumn = false;
        try {
            while (c.moveToNext()) {
                if ("modem_reported_at".equals(c.getString(1))) {
                    hasColumn = true;
                }
            }
        } finally {
            c.close();
        }
        assertTrue("v2 must add modem_reported_at", hasColumn);

        // Nothing lost: the uploaded row and both unsent rows are all still here.
        assertEquals(3, store.countAll());
        assertEquals(1, store.countUploaded());
        assertEquals(2, store.countUnsent());
        store.close();
    }

    @Test
    public void migratedRowsReadBackWithANullModemTimestamp() {
        Context ctx = RuntimeEnvironment.getApplication();
        seedV1Database(ctx);

        SampleStore store = new SampleStore(ctx);
        // The uploader's read path must tolerate the pre-v2 rows it will now find:
        // null modem stamp, everything else intact, so they still upload.
        List<Sample> unsent = store.unsentForSession("session-live", 100);
        assertEquals(2, unsent.size());
        for (Sample s : unsent) {
            assertNull("a pre-v2 row genuinely has no modem timestamp", s.modemReportedAt);
            assertEquals("LTE", s.networkType);
            assertEquals(-10.0, s.rsrq, 1e-9);
        }
        store.close();
    }

    @Test
    public void afterUpgradeNewRowsStoreTheirModemTimestamp() {
        Context ctx = RuntimeEnvironment.getApplication();
        seedV1Database(ctx);

        SampleStore store = new SampleStore(ctx);
        Sample s = new Sample();
        s.sampleId = "s-new";
        s.sessionId = "session-live";
        s.recordedAt = "2026-07-25T10:00:00Z";
        s.modemReportedAt = "2026-07-25T09:59:58Z";
        s.lat = 32.41;
        s.lon = -81.76;
        s.networkType = "LTE";
        s.rsrp = -99.0;
        store.insert(s);

        for (Sample got : store.unsentForSession("session-live", 100)) {
            if ("s-new".equals(got.sampleId)) {
                assertEquals("2026-07-25T09:59:58Z", got.modemReportedAt);
                store.close();
                return;
            }
        }
        throw new AssertionError("the newly inserted sample was not read back");
    }

    @Test
    public void openingAFreshInstallCreatesV2Directly() {
        // No seeded database: onCreate must already include the column, or a new
        // install would diverge from an upgraded one.
        Context ctx = RuntimeEnvironment.getApplication();
        SampleStore store = new SampleStore(ctx);
        SQLiteDatabase db = store.getWritableDatabase();
        assertEquals(2, db.getVersion());
        Cursor c = db.rawQuery("SELECT modem_reported_at FROM samples LIMIT 0", null);
        try {
            assertEquals(1, c.getColumnCount());
        } finally {
            c.close();
        }
        store.close();
    }
}
