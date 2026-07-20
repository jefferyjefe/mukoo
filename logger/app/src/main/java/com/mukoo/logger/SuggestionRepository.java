package com.mukoo.logger;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;

import com.mukoo.logger.map.Suggestion;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

// The phone's source of drive suggestions. Mirrors the app's store-and-forward
// philosophy, but in the read direction: fetch the current suggestions GeoJSON
// from GET /v1/suggestions when online and cache it to a local file; when
// offline, serve whatever was last cached. So the suggestion layer keeps working
// in the field with no connectivity, exactly like the history layer does.
//
// The suggestions themselves are produced server-side by the model package
// (active learning over the kriging uncertainty surface); the phone only reads
// them. Never throws — a failed fetch just leaves the last cache in place.
public class SuggestionRepository {

    private static final String ENDPOINT = BuildConfig.MUKOO_BASE_URL + "/v1/suggestions";
    // last-fetched GeoJSON, in app-private storage so it survives offline.
    private static final String CACHE_FILE = "suggestions.geojson";
    private static final String CACHE_TMP = "suggestions.geojson.tmp";
    // the server's ETag for the cached body: sent back as If-None-Match so an
    // unchanged suggestion set costs a 304, not a re-download over a rural link.
    private static final String ETAG_FILE = "suggestions.etag";

    private final Context context;

    public SuggestionRepository(Context context) {
        this.context = context.getApplicationContext();
    }

    // Store-and-forward read: refresh the cache from the network if we can, then
    // return the parsed suggestions (fresh if the fetch worked, last-known if it
    // didn't or we're offline). Do NOT call on the UI thread — it does I/O.
    public List<Suggestion> refresh() {
        if (isOnline()) {
            fetchToCache();
        }
        return loadCached();
    }

    // Parse whatever is currently cached. Returns an empty list if nothing has
    // been fetched yet or the cache is unreadable/corrupt.
    public List<Suggestion> loadCached() {
        File f = new File(context.getFilesDir(), CACHE_FILE);
        if (!f.isFile()) {
            return Collections.emptyList();
        }
        try {
            byte[] bytes = new byte[(int) f.length()];
            try (InputStream in = new java.io.FileInputStream(f)) {
                int off = 0, r;
                while (off < bytes.length && (r = in.read(bytes, off, bytes.length - off)) != -1) {
                    off += r;
                }
            }
            return parse(new String(bytes, StandardCharsets.UTF_8));
        } catch (Exception e) {
            return Collections.emptyList();
        }
    }

    // GET the current suggestions and overwrite the cache. Writes to a temp file
    // and renames, so a fetch that dies mid-body can never leave a half-written
    // cache that then parses to garbage. Silent on failure by design.
    private void fetchToCache() {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(ENDPOINT);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(10000);
            conn.setReadTimeout(20000);
            conn.setRequestProperty("Accept", "application/geo+json, application/json");
            // conditional GET: only pay for the body when the set changed. Only
            // valid while we still hold the cache the ETag describes.
            String etag = readSmallFile(ETAG_FILE);
            if (etag != null && new File(context.getFilesDir(), CACHE_FILE).isFile()) {
                conn.setRequestProperty("If-None-Match", etag);
            }

            int code = conn.getResponseCode();
            // 304 == our cache IS the current set; nothing to download.
            if (code == HttpURLConnection.HTTP_NOT_MODIFIED) {
                return;
            }
            // 404 == no suggestions generated yet; anything but 200 -> keep the
            // existing cache untouched.
            if (code != 200) {
                return;
            }

            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            try (InputStream in = conn.getInputStream()) {
                byte[] chunk = new byte[8192];
                int r;
                while ((r = in.read(chunk)) != -1) {
                    buf.write(chunk, 0, r);
                }
            }
            String body = new String(buf.toByteArray(), StandardCharsets.UTF_8);
            // validate before committing to cache: never persist a body we can't
            // parse (would blank the layer on the next offline load).
            if (parse(body).isEmpty() && !looksLikeEmptyCollection(body)) {
                return;
            }

            File tmp = new File(context.getFilesDir(), CACHE_TMP);
            try (java.io.FileOutputStream os = new java.io.FileOutputStream(tmp)) {
                os.write(body.getBytes(StandardCharsets.UTF_8));
            }
            File dest = new File(context.getFilesDir(), CACHE_FILE);
            if (!tmp.renameTo(dest)) {
                // rename can fail if dest exists on some filesystems; replace it.
                //noinspection ResultOfMethodCallIgnored
                dest.delete();
                //noinspection ResultOfMethodCallIgnored
                tmp.renameTo(dest);
            }
            // remember the validator for the body we just cached (best-effort:
            // a missing ETag simply means full downloads until the next 200).
            String newTag = conn.getHeaderField("ETag");
            writeSmallFile(ETAG_FILE, newTag);
        } catch (Exception e) {
            // offline, timeout, server down — the cache (if any) stays valid.
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    private String readSmallFile(String name) {
        File f = new File(context.getFilesDir(), name);
        if (!f.isFile()) {
            return null;
        }
        try (InputStream in = new java.io.FileInputStream(f)) {
            byte[] bytes = new byte[(int) f.length()];
            int off = 0, r;
            while (off < bytes.length && (r = in.read(bytes, off, bytes.length - off)) != -1) {
                off += r;
            }
            String s = new String(bytes, StandardCharsets.UTF_8).trim();
            return s.isEmpty() ? null : s;
        } catch (Exception e) {
            return null;
        }
    }

    private void writeSmallFile(String name, String value) {
        File f = new File(context.getFilesDir(), name);
        try {
            if (value == null) {
                //noinspection ResultOfMethodCallIgnored
                f.delete();
                return;
            }
            try (java.io.FileOutputStream os = new java.io.FileOutputStream(f)) {
                os.write(value.getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception e) {
            // best-effort; a lost ETag only costs one full re-download.
        }
    }

    // A valid FeatureCollection with zero features is legitimate (the model may
    // have produced no drivable targets); distinguish it from a parse failure so
    // we still cache it rather than treating it as garbage.
    private boolean looksLikeEmptyCollection(String body) {
        try {
            JSONObject root = new JSONObject(body);
            JSONArray feats = root.optJSONArray("features");
            return feats != null && feats.length() == 0;
        } catch (Exception e) {
            return false;
        }
    }

    // Parse a suggestions FeatureCollection into ranked Suggestions. Tolerant of
    // missing optional properties; skips any feature without a usable point.
    static List<Suggestion> parse(String geojson) {
        List<Suggestion> out = new ArrayList<>();
        try {
            JSONObject root = new JSONObject(geojson);
            JSONArray features = root.optJSONArray("features");
            if (features == null) {
                return out;
            }
            for (int i = 0; i < features.length(); i++) {
                JSONObject feat = features.optJSONObject(i);
                if (feat == null) {
                    continue;
                }
                JSONObject geom = feat.optJSONObject("geometry");
                JSONArray coords = geom == null ? null : geom.optJSONArray("coordinates");
                if (coords == null || coords.length() < 2) {
                    continue; // no point -> nothing to place
                }
                double lon = coords.optDouble(0, Double.NaN);
                double lat = coords.optDouble(1, Double.NaN);
                if (Double.isNaN(lon) || Double.isNaN(lat)) {
                    continue;
                }
                JSONObject props = feat.optJSONObject("properties");
                int rank = props == null ? (i + 1) : props.optInt("rank", i + 1);
                double stddev = props == null ? Double.NaN : props.optDouble("stddev", Double.NaN);
                String road = props == null ? null : props.optString("road_name", null);
                if (road != null && (road.isEmpty() || "null".equals(road))) {
                    road = null;
                }
                int visitOrder = props == null ? 0 : props.optInt("visit_order", 0);
                out.add(new Suggestion(rank, lat, lon, stddev, road, visitOrder));
            }
        } catch (Exception e) {
            return new ArrayList<>();
        }
        // draw/label in rank order regardless of file order.
        Collections.sort(out, new Comparator<Suggestion>() {
            @Override
            public int compare(Suggestion a, Suggestion b) {
                return Integer.compare(a.rank, b.rank);
            }
        });
        return out;
    }

    private boolean isOnline() {
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
}
