package com.mukoo.logger;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import com.mukoo.logger.map.Suggestion;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;

import java.util.List;

// Runs on Robolectric so parse() executes against Android's actual org.json —
// the implementation whose quirks the guards in parse() exist for (a GeoJSON
// null arrives as JSONObject.NULL and optString turns it into the string
// "null", which must become a Java null, never a road called "null").
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class SuggestionRepositoryParseTest {

    private static String feature(int rank, String roadNameJson, int visitOrder) {
        return "{\"type\":\"Feature\","
                + "\"geometry\":{\"type\":\"Point\",\"coordinates\":[-81.668,32.441]},"
                + "\"properties\":{\"rank\":" + rank + ",\"metric\":\"rsrp\","
                + "\"stddev\":8.49,\"stddev_unit\":\"dBm\","
                + "\"score\":20126.1,\"visit_order\":" + visitOrder + ","
                + "\"road_name\":" + roadNameJson + ",\"road_distance_m\":162.0}}";
    }

    private static String collection(String... features) {
        return "{\"type\":\"FeatureCollection\",\"properties\":{\"count\":"
                + features.length + "},\"features\":[" + String.join(",", features) + "]}";
    }

    @Test
    public void parsesFullFeature() {
        List<Suggestion> out =
                SuggestionRepository.parse(collection(feature(1, "\"Burkhalter Road\"", 3)));
        assertEquals(1, out.size());
        Suggestion s = out.get(0);
        assertEquals(1, s.rank);
        assertEquals(32.441, s.lat, 1e-9);
        assertEquals(-81.668, s.lon, 1e-9);
        assertEquals(8.49, s.stddev, 1e-9);
        assertEquals("Burkhalter Road", s.roadName);
        assertEquals(3, s.visitOrder);
    }

    @Test
    public void jsonNullRoadNameBecomesJavaNull() {
        // the real GeoJSON contains "road_name": null for unnamed OSM ways;
        // Android's optString would stringify it to "null" without the guard.
        List<Suggestion> out =
                SuggestionRepository.parse(collection(feature(1, "null", 1)));
        assertEquals(1, out.size());
        assertNull(out.get(0).roadName);
    }

    @Test
    public void sortsByRankRegardlessOfFileOrder() {
        List<Suggestion> out = SuggestionRepository.parse(collection(
                feature(3, "\"C\"", 1), feature(1, "\"A\"", 2), feature(2, "\"B\"", 3)));
        assertEquals(3, out.size());
        assertEquals(1, out.get(0).rank);
        assertEquals(2, out.get(1).rank);
        assertEquals(3, out.get(2).rank);
    }

    @Test
    public void skipsFeatureWithoutGeometry() {
        String broken = "{\"type\":\"Feature\",\"properties\":{\"rank\":1}}";
        List<Suggestion> out =
                SuggestionRepository.parse(collection(broken, feature(2, "\"B\"", 1)));
        assertEquals(1, out.size());
        assertEquals(2, out.get(0).rank);
    }

    @Test
    public void missingVisitOrderDefaultsToZero() {
        String f = "{\"type\":\"Feature\","
                + "\"geometry\":{\"type\":\"Point\",\"coordinates\":[-81.7,32.4]},"
                + "\"properties\":{\"rank\":1,\"stddev\":8.0,\"road_name\":\"X\"}}";
        List<Suggestion> out = SuggestionRepository.parse(collection(f));
        assertEquals(1, out.size());
        assertEquals(0, out.get(0).visitOrder);
    }

    @Test
    public void malformedJsonYieldsEmptyList() {
        assertTrue(SuggestionRepository.parse("{ not json").isEmpty());
        assertTrue(SuggestionRepository.parse("{\"type\":\"FeatureCollection\"}").isEmpty());
    }
}
