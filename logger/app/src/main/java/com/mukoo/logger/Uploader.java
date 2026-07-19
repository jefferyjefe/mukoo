package com.mukoo.logger;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.GZIPOutputStream;

// drains the local buffer to POST /v1/measurements, one session-batch at a time.
// safe to call over and over: sample_id is the server's idempotency key, so a
// retry after a half-delivered batch just re-skips rows it already stored.
public class Uploader {

    // single carrier for now.
    private static final String CARRIER = "Verizon";
    // stay well under the server's max_batch (5000) and keep request bodies small
    // over a flaky rural link.
    private static final int BATCH_SIZE = 200;
    private static final String ENDPOINT = BuildConfig.MUKOO_BASE_URL + "/v1/measurements";

    private final Context context;
    private final SampleStore store;

    public Uploader(Context context, SampleStore store) {
        this.context = context.getApplicationContext();
        this.store = store;
    }

    public boolean isOnline() {
        ConnectivityManager cm =
                (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) {
            return false;
        }
        Network n = cm.getActiveNetwork();
        if (n == null) {
            return false;
        }
        NetworkCapabilities caps = cm.getNetworkCapabilities(n);
        return caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
    }

    // flush as many full batches as we can. stops on the first failure so unsent
    // rows stay put for the next attempt. this is the whole store-and-forward
    // contract: offline just means the buffer grows until a link comes back.
    public void flush() {
        if (!isOnline()) {
            return;
        }
        String session;
        while ((session = store.nextUnsentSession()) != null) {
            List<Sample> batch = store.unsentForSession(session, BATCH_SIZE);
            if (batch.isEmpty()) {
                break;
            }
            if (!postBatch(session, batch)) {
                break;
            }
            List<String> ids = new ArrayList<>(batch.size());
            for (Sample s : batch) {
                ids.add(s.sampleId);
            }
            store.markUploaded(ids);
        }
    }

    private boolean postBatch(String sessionId, List<Sample> samples) {
        HttpURLConnection conn = null;
        try {
            JSONObject body = new JSONObject();
            body.put("session_id", sessionId);
            body.put("carrier", CARRIER);
            JSONArray arr = new JSONArray();
            for (Sample s : samples) {
                arr.put(sampleJson(s));
            }
            body.put("samples", arr);

            URL url = new URL(ENDPOINT);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(10000);
            conn.setReadTimeout(20000);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            // a 200-sample json batch compresses ~10x; over a flaky rural link a
            // smaller body means fewer mid-transfer failures. the server
            // decompresses on Content-Encoding: gzip.
            conn.setRequestProperty("Content-Encoding", "gzip");

            byte[] payload = body.toString().getBytes(StandardCharsets.UTF_8);
            try (OutputStream os = new GZIPOutputStream(conn.getOutputStream())) {
                os.write(payload);
            }

            int code = conn.getResponseCode();
            // 200 == accepted (the server dedupes internally and reports counts).
            // anything else: leave the rows unsent and try again next flush.
            return code == 200;
        } catch (Exception e) {
            return false;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    // exactly the fields the ingest schema allows and no more: it is configured to
    // forbid unknown keys, so local-only bookkeeping (session_id, carrier, the
    // uploaded flag) must stay out. session_id and carrier ride at batch level.
    private JSONObject sampleJson(Sample s) throws JSONException {
        JSONObject o = new JSONObject();
        o.put("sample_id", s.sampleId);
        o.put("recorded_at", s.recordedAt);
        o.put("lat", s.lat);
        o.put("lon", s.lon);
        o.put("network_type", s.networkType);
        o.put("rsrp", s.rsrp == null ? JSONObject.NULL : s.rsrp);
        o.put("rsrq", s.rsrq == null ? JSONObject.NULL : s.rsrq);
        o.put("sinr", s.sinr == null ? JSONObject.NULL : s.sinr);
        o.put("cell_id", s.cellId == null ? JSONObject.NULL : s.cellId);
        o.put("speed_mps", s.speedMps == null ? JSONObject.NULL : s.speedMps);
        o.put("heading_deg", s.headingDeg == null ? JSONObject.NULL : s.headingDeg);
        return o;
    }
}
