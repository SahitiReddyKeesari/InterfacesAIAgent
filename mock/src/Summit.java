import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.UUID;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Dashboard B - "Summit Servicing", a Java/Struts-style surface.
 *
 * A deliberately different legacy dialect from Meridian, so the surface abstraction has
 * to earn its keep rather than being fitted to one app:
 *   - real form posts to *.do actions, not __doPostBack
 *   - a Struts synchronizer token that must round-trip, else the session is terminated
 *   - an iframe workspace instead of a frameset
 *   - nested tables, <font> tags, spacer gifs, inset borders
 *
 * It also renames every business concept while keeping the same underlying flow -
 * member becomes "customer", sub-account becomes "related account", branch becomes
 * "servicing office", Submit becomes "Post". Any locator strategy that memorised
 * Meridian's wording breaks here, which is the point: it stands in for two institutions
 * running differently configured software.
 */
final class Summit {

    static final String TOKEN_FIELD = "org.apache.struts.taglib.html.TOKEN";
    private static final byte[] SECRET = "summit-struts-token-key".getBytes(StandardCharsets.UTF_8);
    private static final byte[] SPACER = Base64.getDecoder().decode(
            "R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==");

    static String token() {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(SECRET, "HmacSHA256"));
            StringBuilder sb = new StringBuilder();
            for (byte b : mac.doFinal("session".getBytes(StandardCharsets.UTF_8))) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static boolean valid(String tok) {
        return tok != null && !tok.isEmpty() && java.security.MessageDigest.isEqual(
                tok.getBytes(StandardCharsets.UTF_8), token().getBytes(StandardCharsets.UTF_8));
    }

    private static final String CSS = """
        <style type="text/css">
        body{margin:0;background:#eeeee6;font-family:Verdana,Geneva,sans-serif;font-size:10px;color:#333}
        table{border-collapse:collapse}
        .outer{border:2px groove #b0b0a0;background:#f8f8f2}
        .tbar{background:#4a6741;color:#fff;font-weight:bold;padding:4px 8px;letter-spacing:.5px}
        .sect{background:#c8d3c0;font-weight:bold;padding:3px 8px;border-top:1px solid #fff;border-bottom:1px solid #98a890}
        td{padding:2px 6px}
        .cap{text-align:right;color:#444;white-space:nowrap}
        input[type=text],select{border:2px inset #d0d0c8;font-family:Verdana;font-size:10px}
        .b{border:2px outset #d8d8d0;background:#e4e4dc;font-family:Verdana;font-size:10px;padding:1px 8px}
        .lst{border:1px solid #98a890;width:100%}
        .lst th{background:#c8d3c0;border:1px solid #98a890;padding:3px 6px;text-align:left}
        .lst td{border:1px solid #d8d8d0}
        .msg{color:#8b0000;font-weight:bold;padding:4px 8px}
        </style>""";

    static Http.Res spacer() {
        return Http.Res.raw("image/gif", SPACER);
    }

    static Http.Res shell() {
        return Http.Res.html(CSS + """
            <body>
            <table width="100%" cellpadding="0" cellspacing="0"><tr><td class="tbar">
              SUMMIT SERVICING &nbsp;<font color="#c0d0b8">r7.2.14</font>
              <font color="#c0d0b8" style="float:right">Teller: 4471 &nbsp;Office: 002</font>
            </td></tr></table>
            <table width="100%" cellpadding="0" cellspacing="0"><tr>
              <td width="150" valign="top" style="background:#dfe4d8;padding:6px">
                <table cellpadding="0" cellspacing="0" width="100%">
                  <tr><td class="sect">Customer</td></tr>
                  <tr><td><a href="/summit/memberSearch.do" target="workspace">Customer Lookup</a></td></tr>
                  <tr><td><a href="#">Relationship Summary</a></td></tr>
                  <tr><td class="sect">Operations</td></tr>
                  <tr><td><a href="#">Batch Journal</a></td></tr>
                  <tr><td><a href="#">Office Totals</a></td></tr>
                </table>
              </td>
              <td valign="top" style="padding:6px">
                <iframe name="workspace" id="workspace" src="/summit/memberSearch.do"
                        width="100%" height="560" frameborder="0"></iframe>
              </td>
            </tr></table>
            </body>""");
    }

    static Http.Res search(String term, List<Data.Member> results, String message) {
        String msgHtml = message == null ? ""
                : "<tr><td colspan=\"4\" class=\"msg\">" + Http.esc(message) + "</td></tr>";

        String listHtml = "";
        if (results != null) {
            StringBuilder rows = new StringBuilder();
            for (int i = 0; i < results.size(); i++) {
                Data.Member m = results.get(i);
                rows.append("""
                    <tr>
                      <td><input type="submit" name="selectCustomer" value="Open" class="b"
                                 onclick="document.customerSearchForm.selectedIdx.value='${i}'"></td>
                      <td>${id}</td><td>${sur}, ${giv}</td><td>${status}</td><td>${office}</td><td>${n}</td>
                    </tr>""".replace("${i}", String.valueOf(i))
                            .replace("${id}", m.memberId)
                            .replace("${sur}", Http.esc(m.lastName))
                            .replace("${giv}", Http.esc(m.firstName))
                            .replace("${status}", m.status)
                            .replace("${office}", m.branch)
                            .replace("${n}", String.valueOf(m.accounts.size())));
            }
            listHtml = """
                <br>
                <table class="outer" width="100%" cellpadding="0" cellspacing="0"><tr><td>
                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr><td class="sect">Retrieved Customers &mdash; ${n} row(s)</td></tr>
                    <tr><td>
                      <table class="lst" cellpadding="0" cellspacing="0">
                        <tr><th>&nbsp;</th><th>Customer Nbr</th><th>Surname, Given</th>
                            <th>Rel Status</th><th>Office</th><th>Rel Accts</th></tr>
                        ${rows}
                      </table>
                    </td></tr>
                  </table>
                </td></tr></table>""".replace("${n}", String.valueOf(results.size()))
                                     .replace("${rows}", rows.toString());
        }

        String body = CSS + """
            <body>
            <form name="customerSearchForm" method="post" action="/summit/memberSearch.do">
            <input type="hidden" name="${tokenField}" value="${token}">
            <table class="outer" width="100%" cellpadding="0" cellspacing="0"><tr><td>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="sect" colspan="4">Customer Lookup</td></tr>
                <tr><td colspan="4"><img src="/summit/spacer.gif" width="1" height="1" border="0" alt=""></td></tr>
                <tr>
                  <td class="cap"><font face="Verdana">Customer Nbr / Surname:</font></td>
                  <td><input type="text" name="customerNbr" size="22" maxlength="32" value="${term}"></td>
                  <td class="cap"><font face="Verdana">Servicing Office:</font></td>
                  <td><select name="officeCd">
                        <option value="">-- all --</option>
                        <option value="002">002 Sweetwater</option>
                        <option value="004">004 Escalante</option>
                        <option value="009">009 Las Mudas</option>
                      </select></td>
                </tr>
                <tr><td colspan="4">
                  <input type="submit" name="submitAction" value="Retrieve" class="b">
                  <input type="submit" name="submitAction" value="Reset" class="b">
                </td></tr>
                ${message}
              </table>
            </td></tr></table>
            ${list}
            <input type="hidden" name="selectedIdx" value="">
            </form>
            </body>""";
        return Http.Res.html(Http.render(body, "tokenField", TOKEN_FIELD, "token", token(),
                                         "term", Http.esc(term), "message", msgHtml,
                                         "list", listHtml));
    }

    static Http.Res detail(Data.Member m) {
        return detail(m, null);
    }

    static Http.Res detail(Data.Member m, String message) {
        StringBuilder rows = new StringBuilder();
        for (Data.Account a : m.accounts) {
            rows.append("<tr><td>").append(a.number).append("</td><td>").append(a.kind)
                .append("</td><td align=\"right\">").append(Data.money(a.balance))
                .append("</td><td>").append(a.status).append("</td></tr>");
        }
        String msgHtml = message == null ? ""
                : "<tr><td colspan=\"4\" class=\"msg\">" + Http.esc(message) + "</td></tr>";
        String body = CSS + """
            <body>
            <form name="customerDetailForm" method="post" action="/summit/customerDetail.do">
            <input type="hidden" name="${tokenField}" value="${token}">
            <input type="hidden" name="customerNbr" value="${id}">
            <table class="outer" width="100%" cellpadding="0" cellspacing="0"><tr><td>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="sect" colspan="4">Customer Record &mdash; ${id}</td></tr>
                <tr><td class="cap">Surname, Given:</td><td><b>${sur}, ${giv}</b></td>
                    <td class="cap">Rel Status:</td><td>${status}</td></tr>
                <tr><td class="cap">Tax Identification:</td><td>${tin}</td>
                    <td class="cap">Servicing Office:</td><td>${office}</td></tr>
                ${message}
              </table>
            </td></tr></table>
            <br>
            <table class="outer" width="100%" cellpadding="0" cellspacing="0"><tr><td>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="sect">Related Accounts</td></tr>
                <tr><td>
                  <table class="lst" cellpadding="0" cellspacing="0">
                    <tr><th>Acct Nbr</th><th>Product</th><th>Ledger Balance</th><th>Status</th></tr>
                    ${rows}
                  </table>
                </td></tr>
                <tr><td>
                  <input type="submit" name="submitAction" value="Add Related Account" class="b">
                  <input type="submit" name="submitAction" value="Return" class="b">
                </td></tr>
              </table>
            </td></tr></table>
            </form>
            </body>""";
        return Http.Res.html(Http.render(body, "tokenField", TOKEN_FIELD, "token", token(),
                "id", m.memberId, "sur", Http.esc(m.lastName), "giv", Http.esc(m.firstName),
                "status", m.status, "tin", m.maskedSsn(), "office", m.branch,
                "rows", rows.toString(), "message", msgHtml));
    }

    static Http.Res addAccount(Data.Member m, String product, String amount, List<String> errors) {
        String errHtml = "";
        if (errors != null && !errors.isEmpty()) {
            StringBuilder sb = new StringBuilder("<tr><td colspan=\"2\" class=\"msg\">");
            for (String e : errors) {
                sb.append("&raquo; ").append(Http.esc(e)).append("<br>");
            }
            errHtml = sb.append("</td></tr>").toString();
        }
        String opts = "";
        for (String[] kv : new String[][] {{"Savings", "SAV Savings"},
                                           {"Checking", "DDA Checking"},
                                           {"Certificate", "CTF Certificate"}}) {
            opts += "<option value=\"" + kv[0] + "\"" + (kv[0].equals(product) ? " selected" : "")
                    + ">" + kv[1] + "</option>";
        }
        String body = CSS + """
            <body>
            <form name="relatedAccountForm" method="post" action="/summit/relatedAccount.do">
            <input type="hidden" name="${tokenField}" value="${token}">
            <input type="hidden" name="customerNbr" value="${id}">
            <table class="outer" width="100%" cellpadding="0" cellspacing="0"><tr><td>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="sect" colspan="2">Add Related Account &mdash; ${sur}, ${giv}</td></tr>
                <tr><td class="cap">Product Code:</td>
                    <td><select name="productCd"><option value="">-- select --</option>${opts}</select></td></tr>
                <tr><td class="cap">Opening Amount:</td>
                    <td><input type="text" name="openingAmt" size="14" maxlength="12" value="${amt}"></td></tr>
                ${errors}
                <tr><td colspan="2">
                  <input type="submit" name="submitAction" value="Post" class="b">
                  <input type="submit" name="submitAction" value="Abandon" class="b">
                </td></tr>
              </table>
            </td></tr></table>
            </form>
            </body>""";
        return Http.Res.html(Http.render(body, "tokenField", TOKEN_FIELD, "token", token(),
                "id", m.memberId, "sur", Http.esc(m.lastName), "giv", Http.esc(m.firstName),
                "opts", opts, "amt", Http.esc(amount), "errors", errHtml));
    }

    static Http.Res posted(Data.Account a) {
        String body = CSS + """
            <body>
            <table class="outer" width="100%" cellpadding="0" cellspacing="0"><tr><td>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="sect" colspan="2">Transaction Posted</td></tr>
                <tr><td colspan="2"><b>Related account established.</b></td></tr>
                <tr><td class="cap">Acct Nbr:</td><td><b>${num}</b></td></tr>
                <tr><td class="cap">Product:</td><td>${kind}</td></tr>
                <tr><td class="cap">Ledger Balance:</td><td>${bal}</td></tr>
                <tr><td colspan="2"><a href="/summit/memberSearch.do" class="b">Return to Lookup</a></td></tr>
              </table>
            </td></tr></table>
            </body>""";
        return Http.Res.html(Http.render(body, "num", a.number, "kind", a.kind,
                                         "bal", Data.money(a.balance)));
    }

    static Http.Res timeout() {
        return Http.Res.html(CSS + """
            <body>
            <table class="outer" width="100%" cellpadding="0" cellspacing="0"><tr><td>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="sect">Session Terminated</td></tr>
                <tr><td>Inactivity timeout (SUM-3390). The transaction token is no longer valid.</td></tr>
                <tr><td><a href="/summit/memberSearch.do" class="b">Re-establish Session</a></td></tr>
              </table>
            </td></tr></table>
            </body>""");
    }

    static Http.Res interstitial() {
        return Http.Res.html(CSS + """
            <body>
            <table class="outer" width="100%" cellpadding="0" cellspacing="0"><tr><td>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="sect">Operator Advisory</td></tr>
                <tr><td>Nightly posting begins 23:00 ET. Acknowledge to proceed.</td></tr>
                <tr><td><a href="/summit/memberSearch.do" class="b">Acknowledge</a></td></tr>
              </table>
            </td></tr></table>
            </body>""");
    }

    static Http.Res serverError() {
        String body = CSS + """
            <body>
            <table class="outer" width="100%" cellpadding="0" cellspacing="0"><tr><td>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="sect" style="background:#8b0000;color:#fff">Application Error</td></tr>
                <tr><td><pre style="font-size:10px">javax.servlet.ServletException: ServletExecute threw exception
              at org.apache.struts.action.RequestProcessor.process(RequestProcessor.java:279)
              ref=${ref}</pre></td></tr>
              </table>
            </td></tr></table>
            </body>""";
        return Http.Res.html(500, Http.render(body, "ref",
                UUID.randomUUID().toString().replace("-", "").substring(0, 12)));
    }

    /** Shared fault gate. Returns a response to short-circuit with, or null. */
    private static Http.Res guard() {
        if (Faults.fires("server_error")) {
            return serverError();
        }
        if (Faults.fires("session_timeout")) {
            return timeout();
        }
        if (Faults.fires("interstitial")) {
            return interstitial();
        }
        Faults.maybeStall();
        return null;
    }

    static Http.Res handle(Http.Req req) {
        String path = req.path();

        if (path.equals("/summit") || path.equals("/summit/")) {
            return shell();
        }
        if (path.equals("/summit/spacer.gif")) {
            return spacer();
        }
        if (path.equals("/summit/memberSearch.do") && !req.isPost()) {
            return search("", null, null);
        }

        boolean known = path.equals("/summit/memberSearch.do")
                || path.equals("/summit/customerDetail.do")
                || path.equals("/summit/relatedAccount.do");
        if (!known) {
            return null;
        }

        Http.Res blocked = guard();
        if (blocked != null) {
            return blocked;
        }
        if (!valid(req.form(TOKEN_FIELD))) {
            return timeout();
        }

        String term = req.form("customerNbr");
        String action = req.form("submitAction");

        if (path.equals("/summit/memberSearch.do")) {
            if (action.equals("Reset")) {
                return search("", null, null);
            }
            String idx = req.form("selectedIdx");
            if (!req.form("selectCustomer").isEmpty() && idx.matches("\\d+")) {
                List<Data.Member> hits = Data.search(term);
                int i = Integer.parseInt(idx);
                if (i >= 0 && i < hits.size()) {
                    return detail(hits.get(i));
                }
                return search(term, hits, "Selected row is stale. Retrieve again.");
            }
            if (term.isBlank()) {
                return search(term, null, "Customer Nbr or Surname is required.");
            }
            List<Data.Member> hits = Faults.fires("not_found") ? List.of() : Data.search(term);
            if (hits.isEmpty()) {
                return search(term, hits, "No customer records retrieved (SUM-0110).");
            }
            return search(term, hits, null);
        }

        Data.Member m = Data.get(term);
        if (m == null) {
            return search("", List.of(), "Customer context lost (SUM-0114).");
        }

        if (path.equals("/summit/customerDetail.do")) {
            if (action.equals("Return")) {
                return search("", null, null);
            }
            // Same rule as Meridian, in this dialect's wording. The two dashboards
            // disagreeing about who may service a restricted relationship would be a
            // bug, not a tenant difference.
            if (m.status.equals("Restricted")) {
                return detail(m, "Teller not authorized for restricted relationship "
                                 + "(SUM-0917).");
            }
            return addAccount(m, "", "", null);
        }

        // relatedAccount.do
        if (action.equals("Abandon")) {
            return detail(m);
        }
        String product = req.form("productCd");
        String openingAmt = req.form("openingAmt");
        List<String> errors = new ArrayList<>();
        if (Faults.fires("validation_error")) {
            errors.add("Opening amount under product minimum (SUM-2204).");
        }
        if (product.isEmpty()) {
            errors.add("Product Code must be supplied.");
        }
        double amount = 0.0;
        try {
            amount = Double.parseDouble(openingAmt.isEmpty() ? "x" : openingAmt);
            if (amount < 0) {
                errors.add("Opening amount may not be negative.");
            }
        } catch (NumberFormatException e) {
            errors.add("Opening amount is not a valid figure.");
        }
        if (!errors.isEmpty()) {
            return addAccount(m, product, openingAmt, errors);
        }
        return posted(Data.openSubaccount(m.memberId, product, amount));
    }

    private Summit() {
    }
}
