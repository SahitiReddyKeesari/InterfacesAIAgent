import java.io.IOException;
import java.io.InputStream;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

/** Minimal request/response plumbing and a template renderer. */
final class Http {

    /** A parsed request. Handlers take one of these and return a Res. */
    record Req(String method, String path, Map<String, String> query, Map<String, String> form) {
        String form(String name) {
            return form.getOrDefault(name, "");
        }
        boolean isPost() {
            return "POST".equalsIgnoreCase(method);
        }
    }

    record Res(int status, String contentType, byte[] body) {
        static Res html(String body) {
            return html(200, body);
        }
        static Res html(int status, String body) {
            return new Res(status, "text/html; charset=utf-8",
                           body.getBytes(StandardCharsets.UTF_8));
        }
        static Res json(int status, String body) {
            return new Res(status, "application/json",
                           body.getBytes(StandardCharsets.UTF_8));
        }
        static Res raw(String contentType, byte[] body) {
            return new Res(200, contentType, body);
        }
    }

    /**
     * Parse an application/x-www-form-urlencoded body or query string.
     * First value wins; browsers only submit the clicked submit control anyway.
     */
    static Map<String, String> parseForm(String raw) {
        Map<String, String> out = new HashMap<>();
        if (raw == null || raw.isEmpty()) {
            return out;
        }
        for (String pair : raw.split("&")) {
            if (pair.isEmpty()) {
                continue;
            }
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            String v = eq < 0 ? "" : pair.substring(eq + 1);
            out.putIfAbsent(decode(k), decode(v));
        }
        return out;
    }

    static String decode(String s) {
        return URLDecoder.decode(s.replace("+", "%20"), StandardCharsets.UTF_8);
    }

    static String readBody(InputStream in) throws IOException {
        return new String(in.readAllBytes(), StandardCharsets.UTF_8);
    }

    /** Escape text destined for HTML content or an attribute value. */
    static String esc(String s) {
        if (s == null) {
            return "";
        }
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\"", "&quot;");
    }

    /**
     * Substitute ${name} placeholders in a template.
     *
     * Deliberately not String.formatted: the markup is full of literal % (CSS widths)
     * and literal $ (ctl00$ContentPlaceHolder1$...), either of which would have to be
     * escaped throughout. ${name} collides with neither.
     */
    static String render(String template, String... kv) {
        if (kv.length % 2 != 0) {
            throw new IllegalArgumentException("render() needs key/value pairs");
        }
        String out = template;
        for (int i = 0; i < kv.length; i += 2) {
            out = out.replace("${" + kv[i] + "}", kv[i + 1] == null ? "" : kv[i + 1]);
        }
        return out;
    }

    private Http() {
    }
}
