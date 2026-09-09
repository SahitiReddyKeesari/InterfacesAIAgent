import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

/**
 * Mock back-office host serving both legacy dashboards.
 *
 *   /meridian/  Dashboard A - ASP.NET WebForms dialect (frameset, __doPostBack, ViewState)
 *   /summit/    Dashboard B - Java/Struts dialect (.do actions, sync token, iframe)
 *
 * Both expose the same business flow behind different markup, terminology and navigation
 * mechanics, which is what makes them useful as a heterogeneity test.
 *
 * Runs on the JDK's built-in HTTP server so the target app needs no container, no build
 * tool and no third-party jars. A production deployment of this class of app would be a
 * WAR on Tomcat or WebSphere; the handler seam is the same either way.
 *
 * The /__control endpoints are a test harness, not part of the simulated product.
 */
public final class MockBank {

    private static final Pattern NAME = Pattern.compile("\"name\"\\s*:\\s*\"([^\"]*)\"");
    private static final Pattern COUNT = Pattern.compile("\"count\"\\s*:\\s*(\\d+)");

    public static void main(String[] args) throws IOException {
        String host = envOr("MOCKBANK_HOST", "127.0.0.1");
        int port = Integer.parseInt(envOr("MOCKBANK_PORT", "8080"));

        HttpServer server = HttpServer.create(new InetSocketAddress(host, port), 0);
        // A pool, not the default single thread: slow_load must stall one request
        // without freezing the whole app.
        server.setExecutor(Executors.newFixedThreadPool(8));
        server.createContext("/", MockBank::dispatch);
        server.start();
        System.out.println("Mock back-office on http://" + host + ":" + port);
        System.out.println("  Meridian (WebForms dialect) http://" + host + ":" + port + "/meridian/");
        System.out.println("  Summit   (Struts dialect)   http://" + host + ":" + port + "/summit/");
    }

    private static void dispatch(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            Map<String, String> query = Http.parseForm(ex.getRequestURI().getRawQuery());

            // Read the body once, then interpret it per route: the dashboards post
            // form encoding, the control endpoints post JSON. Running JSON through the
            // form parser would url-decode it and mangle any + or % it contained.
            String rawBody = ex.getRequestMethod().equalsIgnoreCase("POST")
                    ? Http.readBody(ex.getRequestBody())
                    : "";
            Map<String, String> form = path.startsWith("/__control/")
                    ? Map.of()
                    : Http.parseForm(rawBody);
            Http.Req req = new Http.Req(ex.getRequestMethod(), path, query, form);

            Http.Res res = route(req, rawBody);
            if (res == null) {
                res = Http.Res.html(404, "<h3>404 Not Found</h3>");
            }
            send(ex, res);
        } catch (RuntimeException e) {
            send(ex, Http.Res.html(500, "<h3>500 " + Http.esc(String.valueOf(e)) + "</h3>"));
        } finally {
            ex.close();
        }
    }

    static Http.Res route(Http.Req req, String rawBody) {
        String path = req.path();

        if (path.equals("/")) {
            return Http.Res.html("""
                <h3 style="font-family:sans-serif">Mock back-office</h3>
                <ul style="font-family:sans-serif">
                  <li><a href="/meridian/">Meridian Core Servicing</a> &mdash; ASP.NET WebForms dialect</li>
                  <li><a href="/summit/">Summit Servicing</a> &mdash; Java/Struts dialect</li>
                </ul>""");
        }

        if (path.startsWith("/__control/")) {
            return control(req, rawBody);
        }
        if (path.startsWith("/meridian")) {
            return Meridian.handle(req);
        }
        if (path.startsWith("/summit")) {
            return Summit.handle(req);
        }
        return null;
    }

    private static Http.Res control(Http.Req req, String rawBody) {
        switch (req.path()) {
            case "/__control/fault" -> {
                Matcher n = NAME.matcher(rawBody);
                Matcher c = COUNT.matcher(rawBody);
                String name = n.find() ? n.group(1) : "";
                int count = c.find() ? Integer.parseInt(c.group(1)) : 1;
                try {
                    Faults.arm(name, count);
                } catch (IllegalArgumentException e) {
                    return Http.Res.json(400, "{\"error\":\"" + Http.esc(e.getMessage())
                            + "\",\"known\":" + jsonArray(Faults.KNOWN.keySet()) + "}");
                }
                return Http.Res.json(200, "{\"armed\":" + jsonCounts() + "}");
            }
            case "/__control/reset" -> {
                Faults.clear();
                Data.reset();
                return Http.Res.json(200, "{\"ok\":true,\"armed\":{}}");
            }
            case "/__control/state" -> {
                StringBuilder tax = new StringBuilder("{");
                for (Map.Entry<String, String> e : Faults.KNOWN.entrySet()) {
                    if (tax.length() > 1) {
                        tax.append(",");
                    }
                    tax.append("\"").append(e.getKey()).append("\":\"").append(e.getValue()).append("\"");
                }
                tax.append("}");
                return Http.Res.json(200,
                        "{\"armed\":" + jsonCounts() + ",\"taxonomy\":" + tax + "}");
            }
            default -> {
                return null;
            }
        }
    }

    private static String jsonCounts() {
        StringBuilder sb = new StringBuilder("{");
        for (Map.Entry<String, Integer> e : Faults.armed().entrySet()) {
            if (sb.length() > 1) {
                sb.append(",");
            }
            sb.append("\"").append(e.getKey()).append("\":").append(e.getValue());
        }
        return sb.append("}").toString();
    }

    private static String jsonArray(Iterable<String> items) {
        StringBuilder sb = new StringBuilder("[");
        for (String s : items) {
            if (sb.length() > 1) {
                sb.append(",");
            }
            sb.append("\"").append(s).append("\"");
        }
        return sb.append("]").toString();
    }

    private static void send(HttpExchange ex, Http.Res res) throws IOException {
        ex.getResponseHeaders().set("Content-Type", res.contentType());
        ex.sendResponseHeaders(res.status(), res.body().length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(res.body());
        }
    }

    private static String envOr(String key, String fallback) {
        String v = System.getenv(key);
        return v == null || v.isBlank() ? fallback : v;
    }

    private MockBank() {
    }
}
