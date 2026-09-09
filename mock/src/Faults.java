import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Injectable runtime faults.
 *
 * The interesting failures in this environment are not layout drift, they are the
 * exceptional states that legitimately occur at runtime. A real demo site will not
 * produce those on demand, so the target app exposes them as switches and replay
 * evidence can summon a specific failure reproducibly.
 */
final class Faults {

    /**
     * Every fault is tagged with the class a replay must report it as:
     *   business - a legitimate answer the caller needs ("no such member")
     *   recover  - transient or dismissable; replay should handle it and continue
     *   hard     - stop and surface a debuggable error
     */
    static final Map<String, String> KNOWN = new LinkedHashMap<>();

    static {
        KNOWN.put("not_found", "business");
        KNOWN.put("permission_denied", "business");
        KNOWN.put("validation_error", "business");
        KNOWN.put("interstitial", "recover");
        KNOWN.put("slow_load", "recover");
        KNOWN.put("session_timeout", "recover");
        KNOWN.put("server_error", "hard");
    }

    private static final Map<String, Integer> ARMED = new LinkedHashMap<>();

    static synchronized void arm(String name, int count) {
        if (!KNOWN.containsKey(name)) {
            throw new IllegalArgumentException("unknown fault: " + name);
        }
        ARMED.put(name, count);
    }

    static synchronized void clear() {
        ARMED.clear();
    }

    static synchronized Map<String, Integer> armed() {
        return new LinkedHashMap<>(ARMED);
    }

    /** Consume one firing of {@code name} if armed. Not idempotent - it decrements. */
    static synchronized boolean fires(String name) {
        int left = ARMED.getOrDefault(name, 0);
        if (left <= 0) {
            return false;
        }
        if (left == 1) {
            ARMED.remove(name);
        } else {
            ARMED.put(name, left - 1);
        }
        return true;
    }

    /** Simulate a slow backend - the kind a fixed-duration wait would flake against. */
    static void maybeStall() {
        if (fires("slow_load")) {
            try {
                Thread.sleep(6000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    private Faults() {
    }
}
