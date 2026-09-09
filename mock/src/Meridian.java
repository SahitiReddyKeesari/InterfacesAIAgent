import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.List;
import java.util.UUID;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Dashboard A - "Meridian Core Servicing", an ASP.NET WebForms-style surface.
 *
 * Reproduces the properties that make this class of app hard to automate:
 *   - a three-frame shell, so the flow does not live in the top-level document
 *   - every navigation is a form postback via __doPostBack, not a link with an href
 *   - __VIEWSTATE must be echoed back; a request without it is an expired session
 *   - control ids are generated (ctl00$ContentPlaceHolder1$...) and carry row indexes
 *   - table layout, and no <label for=...> - a field's only clue is the text in the
 *     adjacent cell, which is why a CSS selector strategy is a dead end here
 *
 * Flow: Member Inquiry -> Results -> Member Detail -> Open Sub-Account -> Confirmation
 */
final class Meridian {

    private static final byte[] SECRET = "meridian-viewstate-key".getBytes(StandardCharsets.UTF_8);
    private static final String EV = "wEWBAKD3rSKAgKM54rGBgLSwpmHDAK7q7GGCA==";
    private static final String P = "ctl00$ContentPlaceHolder1$";

    // ------------------------------------------------------------------ viewstate

    private static String sign(byte[] payload) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(SECRET, "HmacSHA256"));
            StringBuilder sb = new StringBuilder();
            for (byte b : mac.doFinal(payload)) {
                sb.append(String.format("%02x", b));
            }
            return sb.substring(0, 16);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    static String encodeViewState(String screen, String memberId) {
        return encodeViewState(screen, memberId, "");
    }

    /** State is screen|memberId|extra - enough to survive a postback. */
    static String encodeViewState(String screen, String memberId, String extra) {
        byte[] raw = (screen + "|" + (memberId == null ? "" : memberId)
                + "|" + (extra == null ? "" : extra)).getBytes(StandardCharsets.UTF_8);
        return Base64.getEncoder().encodeToString(raw) + "." + sign(raw);
    }

    /** Returns null when the blob is missing or tampered with, i.e. session expired. */
    static String[] decodeViewState(String blob) {
        if (blob == null || !blob.contains(".")) {
            return null;
        }
        int dot = blob.lastIndexOf('.');
        try {
            byte[] raw = Base64.getDecoder().decode(blob.substring(0, dot));
            if (!sign(raw).equals(blob.substring(dot + 1))) {
                return null;
            }
            String[] parts = new String(raw, StandardCharsets.UTF_8).split("\\|", -1);
            return parts.length == 3 ? parts : null;
        } catch (Exception e) {
            return null;
        }
    }

    // --------------------------------------------------------------------- chrome

    private static final String CSS = """
        <style type="text/css">
        body{margin:0;font-family:Tahoma,Arial,sans-serif;font-size:11px;background:#d4d0c8;color:#000}
        table{border-collapse:collapse}
        .pnl{border:1px solid #7f9db9;background:#fff}
        .hdr{background:#0a246a;color:#fff;font-weight:bold;padding:3px 6px}
        .lbl{padding:3px 6px;text-align:right;white-space:nowrap}
        .fld{padding:3px 6px}
        input[type=text],select{border:1px solid #7f9db9;font-family:Tahoma;font-size:11px;padding:1px}
        .btn{border:1px outset #aca899;background:#ece9d8;padding:2px 10px;text-decoration:none;color:#000;display:inline-block;cursor:default}
        .grid{border:1px solid #7f9db9;width:100%}
        .grid th{background:#ece9d8;border:1px solid #aca899;padding:3px 6px;text-align:left}
        .grid td{border:1px solid #d4d0c8;padding:3px 6px}
        .err{color:#a00;font-weight:bold;padding:4px 6px}
        </style>""";

    private static final String POSTBACK = """
        <div class="aspNetHidden">
        <input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="">
        <input type="hidden" name="__EVENTARGUMENT" id="__EVENTARGUMENT" value="">
        <input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="${viewstate}">
        <input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="C2EE9ABB">
        <input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="${ev}">
        </div>
        <script type="text/javascript">
        var theForm = document.forms['aspnetForm'];
        function __doPostBack(eventTarget, eventArgument) {
            theForm.__EVENTTARGET.value = eventTarget;
            theForm.__EVENTARGUMENT.value = eventArgument;
            theForm.submit();
        }
        </script>""";

    private static String shell(String viewstate, String body) {
        return shell("/meridian/main.aspx", viewstate, body);
    }

    private static String shell(String action, String viewstate, String body) {
        return CSS + "\n<body>\n<form name=\"aspnetForm\" method=\"post\" "
                + "action=\"" + action + "\" id=\"aspnetForm\">\n"
                + Http.render(POSTBACK, "viewstate", viewstate, "ev", EV)
                + body + "\n</form>\n</body>";
    }

    // ---------------------------------------------------------------------- views

    static Http.Res frameset() {
        return Http.Res.html("""
            <html>
            <head><title>Meridian Core Servicing</title></head>
            <frameset rows="52,*" border="1" frameborder="1">
              <frame name="banner" src="/meridian/banner.aspx" scrolling="no" noresize>
              <frameset cols="168,*">
                <frame name="nav" src="/meridian/nav.aspx" scrolling="no">
                <frame name="main" src="/meridian/main.aspx">
              </frameset>
            </frameset>
            <noframes><body>This application requires frame support.</body></noframes>
            </html>""");
    }

    static Http.Res banner() {
        return Http.Res.html(CSS + """
            <body style="background:#0a246a">
            <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="padding:8px 10px"><font color="#ffffff" size="4" face="Tahoma"><b>MERIDIAN</b> Core Servicing</font></td>
            <td align="right" style="padding:8px 10px"><font color="#c9d3ea">Operator: MTHOMPSON &nbsp;|&nbsp; Inst 0417 &nbsp;|&nbsp; Term 08</font></td>
            </tr></table>
            </body>""");
    }

    static Http.Res nav() {
        return Http.Res.html(CSS + """
            <body style="background:#ece9d8">
            <table width="100%" cellpadding="0" cellspacing="0">
            <tr><td class="hdr">Servicing</td></tr>
            <tr><td style="padding:4px 8px"><a href="/meridian/main.aspx" target="main">Member Inquiry</a></td></tr>
            <tr><td style="padding:4px 8px"><a href="#">Transaction Journal</a></td></tr>
            <tr><td style="padding:4px 8px"><a href="#">Holds &amp; Restraints</a></td></tr>
            <tr><td style="padding:4px 8px"><a href="/meridian/cards.aspx" target="main">Card Services</a></td></tr>
            <tr><td class="hdr">Administration</td></tr>
            <tr><td style="padding:4px 8px"><a href="#">Operator Profile</a></td></tr>
            </table>
            </body>""");
    }

    private static final String SEARCH_BODY = """
        <table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:6px">
          <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
            <tr><td class="hdr" colspan="4">Member Inquiry</td></tr>
            <tr>
              <td class="lbl"><span id="ctl00_ContentPlaceHolder1_lblMemberId">Member / Name:</span></td>
              <td class="fld"><input name="ctl00$ContentPlaceHolder1$txtMemberId" type="text" maxlength="32" size="24"
                    id="ctl00_ContentPlaceHolder1_txtMemberId" value="${term}"></td>
              <td class="lbl"><span id="ctl00_ContentPlaceHolder1_lblBranch">Servicing Branch:</span></td>
              <td class="fld"><select name="ctl00$ContentPlaceHolder1$ddlBranch" id="ctl00_ContentPlaceHolder1_ddlBranch">
                    <option value="">(All)</option>
                    <option value="SW">Sweetwater</option>
                    <option value="ES">Escalante</option>
                    <option value="LM">Las Mudas</option>
                  </select></td>
            </tr>
            <tr><td colspan="4" class="fld">
              <a id="ctl00_ContentPlaceHolder1_btnSearch" class="btn"
                 href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnSearch','')">Search</a>
              &nbsp;
              <a id="ctl00_ContentPlaceHolder1_btnClear" class="btn"
                 href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnClear','')">Clear</a>
            </td></tr>
            ${message}
          </table>
          ${results}
        </td></tr></table>""";

    static Http.Res search(String memberId, String term, List<Data.Member> results, String message) {
        String msgHtml = message == null ? "" :
                "<tr><td colspan=\"4\" class=\"err\"><span id=\"ctl00_ContentPlaceHolder1_lblMessage\">"
                + Http.esc(message) + "</span></td></tr>";

        String gridHtml = "";
        if (results != null) {
            StringBuilder rows = new StringBuilder();
            for (int i = 0; i < results.size(); i++) {
                Data.Member m = results.get(i);
                String ctl = String.format("ctl%02d", i + 2);
                rows.append("""
                    <tr>
                      <td><a id="ctl00_ContentPlaceHolder1_gvResults_${ctl}_lnkSelect"
                             href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$gvResults$${ctl}$lnkSelect','')">Select</a></td>
                      <td>${id}</td><td>${name}</td><td>${status}</td><td>${branch}</td><td>${accts}</td>
                    </tr>""".replace("${ctl}", ctl)
                            .replace("${id}", m.memberId)
                            .replace("${name}", Http.esc(m.fullName()))
                            .replace("${status}", m.status)
                            .replace("${branch}", m.branch)
                            .replace("${accts}", String.valueOf(m.accounts.size())));
            }
            gridHtml = """
                <br>
                <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
                  <tr><td class="hdr">Search Results (${n})</td></tr>
                  <tr><td style="padding:6px">
                    <table class="grid" cellpadding="0" cellspacing="0" id="ctl00_ContentPlaceHolder1_gvResults">
                      <tr><th>&nbsp;</th><th>Member</th><th>Name</th><th>Status</th><th>Branch</th><th>Accounts</th></tr>
                      ${rows}
                    </table>
                  </td></tr>
                </table>""".replace("${n}", String.valueOf(results.size()))
                           .replace("${rows}", rows.toString());
        }

        String body = Http.render(SEARCH_BODY, "term", Http.esc(term),
                                  "message", msgHtml, "results", gridHtml);
        return Http.Res.html(shell(encodeViewState("search", memberId), body));
    }

    static Http.Res detail(Data.Member m) {
        StringBuilder rows = new StringBuilder();
        for (Data.Account a : m.accounts) {
            rows.append("<tr><td>").append(a.number).append("</td><td>").append(a.kind)
                .append("</td><td align=\"right\">").append(Data.money(a.balance))
                .append("</td><td>").append(a.status).append("</td></tr>");
        }
        String body = """
            <table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:6px">
              <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="hdr" colspan="4">Member Detail &mdash; ${id}</td></tr>
                <tr><td class="lbl">Name:</td>
                    <td class="fld"><span id="ctl00_ContentPlaceHolder1_lblName">${name}</span></td>
                    <td class="lbl">Status:</td>
                    <td class="fld"><span id="ctl00_ContentPlaceHolder1_lblStatus">${status}</span></td></tr>
                <tr><td class="lbl">Tax ID:</td>
                    <td class="fld"><span id="ctl00_ContentPlaceHolder1_lblTin">${tin}</span></td>
                    <td class="lbl">Servicing Branch:</td>
                    <td class="fld"><span id="ctl00_ContentPlaceHolder1_lblBranch">${branch}</span></td></tr>
              </table>
              <br>
              <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="hdr">Account Relationships</td></tr>
                <tr><td style="padding:6px">
                  <table class="grid" cellpadding="0" cellspacing="0" id="ctl00_ContentPlaceHolder1_gvAccounts">
                    <tr><th>Account</th><th>Type</th><th>Current Balance</th><th>Status</th></tr>
                    ${rows}
                  </table>
                </td></tr>
                <tr><td style="padding:6px">
                  <a id="ctl00_ContentPlaceHolder1_btnOpenSub" class="btn"
                     href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnOpenSub','')">Open Sub-Account</a>
                  &nbsp;
                  <a id="ctl00_ContentPlaceHolder1_btnBack" class="btn"
                     href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnBack','')">Back to Inquiry</a>
                </td></tr>
              </table>
            </td></tr></table>""";
        body = Http.render(body, "id", m.memberId, "name", Http.esc(m.fullName()),
                           "status", m.status, "tin", m.maskedSsn(), "branch", m.branch,
                           "rows", rows.toString());
        return Http.Res.html(shell(encodeViewState("detail", m.memberId), body));
    }

    static Http.Res subaccount(Data.Member m, String acctType, String initial, List<String> errors) {
        String errHtml = "";
        if (errors != null && !errors.isEmpty()) {
            StringBuilder sb = new StringBuilder(
                "<tr><td colspan=\"2\" class=\"err\"><span id=\"ctl00_ContentPlaceHolder1_valSummary\">");
            for (String e : errors) {
                sb.append("&bull; ").append(Http.esc(e)).append("<br>");
            }
            errHtml = sb.append("</span></td></tr>").toString();
        }
        String opts = "";
        for (String k : List.of("Savings", "Checking", "Certificate")) {
            opts += "<option value=\"" + k + "\"" + (k.equals(acctType) ? " selected" : "")
                    + ">" + k + "</option>";
        }
        String body = """
            <table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:6px">
              <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="hdr" colspan="2">Open Sub-Account &mdash; ${id} ${name}</td></tr>
                <tr><td class="lbl">Account Type:</td>
                    <td class="fld"><select name="ctl00$ContentPlaceHolder1$ddlAcctType" id="ctl00_ContentPlaceHolder1_ddlAcctType">
                      <option value="">(Select)</option>${opts}
                    </select></td></tr>
                <tr><td class="lbl">Initial Deposit:</td>
                    <td class="fld"><input name="ctl00$ContentPlaceHolder1$txtInitial" type="text" maxlength="12" size="14"
                          id="ctl00_ContentPlaceHolder1_txtInitial" value="${initial}"></td></tr>
                ${errors}
                <tr><td colspan="2" class="fld">
                  <a id="ctl00_ContentPlaceHolder1_btnSubmit" class="btn"
                     href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnSubmit','')">Submit</a>
                  &nbsp;
                  <a id="ctl00_ContentPlaceHolder1_btnCancel" class="btn"
                     href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnCancel','')">Cancel</a>
                </td></tr>
              </table>
            </td></tr></table>""";
        body = Http.render(body, "id", m.memberId, "name", Http.esc(m.fullName()),
                           "opts", opts, "initial", Http.esc(initial), "errors", errHtml);
        return Http.Res.html(shell(encodeViewState("subaccount", m.memberId), body));
    }

    static Http.Res confirm(String memberId, Data.Account a) {
        String body = """
            <table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:6px">
              <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="hdr" colspan="2">Confirmation</td></tr>
                <tr><td colspan="2" class="fld">
                  <span id="ctl00_ContentPlaceHolder1_lblConfirm"><b>Sub-account opened successfully.</b></span></td></tr>
                <tr><td class="lbl">New Account Number:</td>
                    <td class="fld"><span id="ctl00_ContentPlaceHolder1_lblNewAcct">${num}</span></td></tr>
                <tr><td class="lbl">Account Type:</td>
                    <td class="fld"><span id="ctl00_ContentPlaceHolder1_lblNewType">${kind}</span></td></tr>
                <tr><td class="lbl">Opening Balance:</td>
                    <td class="fld"><span id="ctl00_ContentPlaceHolder1_lblNewBal">${bal}</span></td></tr>
                <tr><td colspan="2" class="fld">
                  <a id="ctl00_ContentPlaceHolder1_btnDone" class="btn"
                     href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnDone','')">Return to Inquiry</a></td></tr>
              </table>
            </td></tr></table>""";
        body = Http.render(body, "num", a.number, "kind", a.kind, "bal", Data.money(a.balance));
        return Http.Res.html(shell(encodeViewState("confirm", memberId), body));
    }

    static Http.Res timeout() {
        return Http.Res.html(CSS + """
            <body>
            <table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:6px">
            <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
              <tr><td class="hdr">Session Expired</td></tr>
              <tr><td class="fld"><span id="ctl00_lblExpired">Your session has timed out due to inactivity
                  (SEC-0442). Sign on again to continue.</span></td></tr>
              <tr><td class="fld"><a id="ctl00_btnSignOn" class="btn" href="/meridian/signon.aspx">Sign On</a></td></tr>
            </table>
            </td></tr></table>
            </body>""");
    }

    static Http.Res interstitial() {
        return Http.Res.html(CSS + """
            <body>
            <table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:6px">
            <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
              <tr><td class="hdr">Notice</td></tr>
              <tr><td class="fld"><span id="ctl00_lblNotice">Scheduled maintenance window begins at 23:00 ET.
                  Acknowledge to continue.</span></td></tr>
              <tr><td class="fld"><a id="ctl00_btnAcknowledge" class="btn" href="/meridian/main.aspx">Acknowledge</a></td></tr>
            </table>
            </td></tr></table>
            </body>""");
    }

    static Http.Res serverError() {
        String body = CSS + """
            <body>
            <table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:6px">
            <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
              <tr><td class="hdr" style="background:#a00">Server Error in '/MeridianCore' Application</td></tr>
              <tr><td class="fld"><span id="ctl00_lblError">Runtime Error &mdash; reference ${ref}.
                  The current request could not be completed.</span></td></tr>
            </table>
            </td></tr></table>
            </body>""";
        return Http.Res.html(500, Http.render(body, "ref",
                UUID.randomUUID().toString().replace("-", "").substring(0, 12)));
    }

    static Http.Res signon() {
        return Http.Res.html(CSS + """
            <body>
            <form name="aspnetForm" method="post" action="/meridian/signon.aspx" id="aspnetForm">
            <table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:6px">
            <table class="pnl" cellpadding="0" cellspacing="0">
              <tr><td class="hdr" colspan="2">Operator Sign On</td></tr>
              <tr><td class="lbl">Operator ID:</td>
                  <td class="fld"><input type="text" name="ctl00$txtOperator" id="ctl00_txtOperator" value="MTHOMPSON"></td></tr>
              <tr><td class="lbl">Password:</td>
                  <td class="fld"><input type="password" name="ctl00$txtPassword" id="ctl00_txtPassword"></td></tr>
              <tr><td colspan="2" class="fld">
                  <input type="submit" name="ctl00$btnSignOn" value="Sign On" id="ctl00_btnSignOn" class="btn"></td></tr>
            </table>
            </td></tr></table>
            </form>
            </body>""");
    }


    // ---------------------------------------------------------------- card services

    /** Which actions a card in a given state offers. Blocked is terminal. */
    private static List<String> actionsFor(String status) {
        return switch (status) {
            case "Inactive" -> List.of("Activate", "Block");
            case "Active" -> List.of("Lock", "Block");
            case "Locked" -> List.of("Unlock", "Block");
            default -> List.of();
        };
    }

    static Http.Res cardServices(String memberId, String term, Data.Member m,
                                 String message, boolean isError) {
        String msgHtml = "";
        if (message != null) {
            msgHtml = "<tr><td colspan=\"4\" class=\"" + (isError ? "err" : "fld")
                    + "\"><span id=\"ctl00_ContentPlaceHolder1_lblCardMsg\">"
                    + (isError ? "" : "<b>") + Http.esc(message) + (isError ? "" : "</b>")
                    + "</span></td></tr>";
        }

        String gridHtml = "";
        if (m != null) {
            StringBuilder rows = new StringBuilder();
            for (int i = 0; i < m.cards.size(); i++) {
                Data.Card c = m.cards.get(i);
                String ctl = String.format("ctl%02d", i + 2);
                StringBuilder links = new StringBuilder();
                for (String a : actionsFor(c.status)) {
                    if (links.length() > 0) {
                        links.append(" &nbsp;|&nbsp; ");
                    }
                    links.append("<a id=\"ctl00_ContentPlaceHolder1_gvCards_").append(ctl)
                         .append("_lnk").append(a).append("\" href=\"javascript:__doPostBack('")
                         .append("ctl00$ContentPlaceHolder1$gvCards$").append(ctl)
                         .append("$lnk").append(a).append("','')\">").append(a).append("</a>");
                }
                if (links.length() == 0) {
                    links.append("&mdash;");
                }
                rows.append("<tr><td>").append(c.masked()).append("</td><td>").append(c.kind)
                    .append("</td><td>").append(c.expiry).append("</td><td>").append(c.status)
                    .append("</td><td>").append(links).append("</td></tr>");
            }
            gridHtml = """
                <br>
                <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
                  <tr><td class="hdr">Cards on Relationship ${id} &mdash; ${name}</td></tr>
                  <tr><td style="padding:6px">
                    <table class="grid" cellpadding="0" cellspacing="0" id="ctl00_ContentPlaceHolder1_gvCards">
                      <tr><th>Card Number</th><th>Type</th><th>Expires</th><th>Status</th><th>Action</th></tr>
                      ${rows}
                    </table>
                  </td></tr>
                </table>""".replace("${id}", m.memberId)
                           .replace("${name}", Http.esc(m.fullName()))
                           .replace("${rows}", rows.toString());
        }

        String body = """
            <table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:6px">
              <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="hdr" colspan="4">Card Services</td></tr>
                <tr>
                  <td class="lbl"><span id="ctl00_ContentPlaceHolder1_lblCardMember">Member / Name:</span></td>
                  <td class="fld"><input name="ctl00$ContentPlaceHolder1$txtCardMember" type="text" maxlength="32" size="24"
                        id="ctl00_ContentPlaceHolder1_txtCardMember" value="${term}"></td>
                  <td class="fld" colspan="2">
                    <a id="ctl00_ContentPlaceHolder1_btnCardSearch" class="btn"
                       href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnCardSearch','')">Retrieve Cards</a>
                  </td>
                </tr>
                ${message}
              </table>
              ${grid}
            </td></tr></table>""";
        body = Http.render(body, "term", Http.esc(term), "message", msgHtml, "grid", gridHtml);
        return Http.Res.html(shell("/meridian/cards.aspx",
                encodeViewState("cards", memberId, ""), body));
    }

    /**
     * Blocking a card is irreversible, so it is gated behind an explicit confirmation
     * step rather than firing straight off the grid link. Activate, Lock and Unlock are
     * reversible and apply immediately.
     */
    static Http.Res blockConfirm(String memberId, Data.Card c) {
        String body = """
            <table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:6px">
              <table class="pnl" width="100%" cellpadding="0" cellspacing="0">
                <tr><td class="hdr" style="background:#a00" colspan="2">Confirm Permanent Block</td></tr>
                <tr><td colspan="2" class="err">
                  <span id="ctl00_ContentPlaceHolder1_lblBlockWarn">This action is permanent and cannot be reversed.
                  The card cannot be reactivated once blocked.</span></td></tr>
                <tr><td class="lbl">Card Number:</td>
                    <td class="fld"><span id="ctl00_ContentPlaceHolder1_lblBlockCard">${masked}</span></td></tr>
                <tr><td class="lbl">Type:</td><td class="fld">${kind}</td></tr>
                <tr><td class="lbl">Current Status:</td><td class="fld">${status}</td></tr>
                <tr><td colspan="2" class="fld">
                  <a id="ctl00_ContentPlaceHolder1_btnConfirmBlock" class="btn"
                     href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnConfirmBlock','')">Confirm Block</a>
                  &nbsp;
                  <a id="ctl00_ContentPlaceHolder1_btnCancelBlock" class="btn"
                     href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnCancelBlock','')">Cancel</a>
                </td></tr>
              </table>
            </td></tr></table>""";
        body = Http.render(body, "masked", c.masked(), "kind", c.kind, "status", c.status);
        return Http.Res.html(shell("/meridian/cards.aspx",
                encodeViewState("blockconfirm", memberId, c.last4()), body));
    }

    private static Http.Res handleCards(Http.Req req) {
        if (!req.isPost()) {
            return cardServices(null, "", null, null, false);
        }
        if (Faults.fires("server_error")) {
            return serverError();
        }
        String[] state = decodeViewState(req.form("__VIEWSTATE"));
        if (state == null || Faults.fires("session_timeout")) {
            return timeout();
        }
        if (Faults.fires("interstitial")) {
            return interstitial();
        }
        Faults.maybeStall();

        String target = req.form("__EVENTTARGET");
        String memberId = state[1];
        String pendingLast4 = state[2];
        String term = req.form(P + "txtCardMember");

        if (target.endsWith("btnCardSearch")) {
            if (term.isBlank()) {
                return cardServices(null, term, null, "Enter a member number or name.", true);
            }
            List<Data.Member> hits = Faults.fires("not_found") ? List.of() : Data.search(term);
            if (hits.isEmpty()) {
                return cardServices(null, term, null,
                        "No member records match the criteria entered.", true);
            }
            Data.Member m = hits.get(0);
            return cardServices(m.memberId, term, m, null, false);
        }

        Data.Member m = Data.get(memberId);

        if (target.contains("gvCards")) {
            int row = Integer.parseInt(target.split("\\$ctl")[1].split("\\$")[0]) - 2;
            if (m == null || row < 0 || row >= m.cards.size()) {
                return cardServices(memberId, term, m, "Selected card is no longer available.", true);
            }
            Data.Card c = m.cards.get(row);
            if (target.endsWith("lnkBlock")) {
                // Irreversible: confirm before doing anything.
                if (m.status.equals("Restricted")) {
                    return cardServices(memberId, term, m,
                            "Operator not authorized for restricted relationship (SEC-0917).", true);
                }
                return blockConfirm(memberId, c);
            }
            String action = target.endsWith("lnkActivate") ? "activate"
                    : target.endsWith("lnkLock") ? "lock"
                    : target.endsWith("lnkUnlock") ? "unlock" : "";
            Data.CardResult r = Data.cardAction(memberId, c.last4(), action);
            return cardServices(memberId, term, Data.get(memberId), r.message(), !r.ok());
        }

        if (target.endsWith("btnConfirmBlock")) {
            Data.CardResult r = Data.cardAction(memberId, pendingLast4, "block");
            return cardServices(memberId, term, Data.get(memberId), r.message(), !r.ok());
        }
        if (target.endsWith("btnCancelBlock")) {
            return cardServices(memberId, term, m, "Block cancelled. No change was made.", false);
        }
        return cardServices(memberId, term, m, null, false);
    }

    // -------------------------------------------------------------------- routing

    static Http.Res handle(Http.Req req) {
        String path = req.path();

        if (path.equals("/meridian") || path.equals("/meridian/")) {
            return frameset();
        }
        if (path.equals("/meridian/banner.aspx")) {
            return banner();
        }
        if (path.equals("/meridian/nav.aspx")) {
            return nav();
        }
        if (path.equals("/meridian/cards.aspx")) {
            return handleCards(req);
        }
        if (path.equals("/meridian/signon.aspx")) {
            return req.isPost() ? search(null, "", null, "Session re-established.") : signon();
        }
        if (!path.equals("/meridian/main.aspx")) {
            return null;
        }
        if (!req.isPost()) {
            return search(null, "", null, null);
        }

        if (Faults.fires("server_error")) {
            return serverError();
        }
        String[] state = decodeViewState(req.form("__VIEWSTATE"));
        if (state == null || Faults.fires("session_timeout")) {
            return timeout();
        }
        if (Faults.fires("interstitial")) {
            return interstitial();
        }
        Faults.maybeStall();

        String target = req.form("__EVENTTARGET");
        String memberId = state[1];
        String term = req.form(P + "txtMemberId");

        if (target.endsWith("btnSearch")) {
            if (term.isBlank()) {
                return search(memberId, term, null, "Enter a member number or name.");
            }
            List<Data.Member> hits = Faults.fires("not_found") ? List.of() : Data.search(term);
            if (hits.isEmpty()) {
                return search(memberId, term, hits,
                              "No member records match the criteria entered.");
            }
            return search(memberId, term, hits, null);
        }
        if (target.endsWith("btnClear")) {
            return search(memberId, "", null, null);
        }
        if (target.contains("gvResults") && target.endsWith("lnkSelect")) {
            int row = Integer.parseInt(target.split("\\$ctl")[1].split("\\$")[0]) - 2;
            List<Data.Member> hits = Data.search(term);
            if (row < 0 || row >= hits.size()) {
                return search(memberId, term, hits, "Selected row is no longer available.");
            }
            return detail(hits.get(row));
        }

        Data.Member m = Data.get(memberId);
        if (target.endsWith("btnOpenSub")) {
            if (m == null) {
                return search(null, "", null, "Member context lost.");
            }
            return m.status.equals("Restricted") ? detail(m) : subaccount(m, "", "", null);
        }
        if (target.endsWith("btnSubmit")) {
            if (m == null) {
                return search(null, "", null, "Member context lost.");
            }
            String acctType = req.form(P + "ddlAcctType");
            String initial = req.form(P + "txtInitial");
            List<String> errors = new java.util.ArrayList<>();
            if (Faults.fires("validation_error")) {
                errors.add("Initial deposit is below the product minimum (PRD-1180).");
            }
            if (acctType.isEmpty()) {
                errors.add("Account Type is required.");
            }
            double amount = 0.0;
            try {
                amount = Double.parseDouble(initial.isEmpty() ? "x" : initial);
                if (amount < 0) {
                    errors.add("Initial Deposit may not be negative.");
                }
            } catch (NumberFormatException e) {
                errors.add("Initial Deposit must be a numeric amount.");
            }
            if (!errors.isEmpty()) {
                return subaccount(m, acctType, initial, errors);
            }
            return confirm(m.memberId, Data.openSubaccount(m.memberId, acctType, amount));
        }
        if (target.endsWith("btnCancel") && m != null) {
            return detail(m);
        }
        return search(memberId, term, null, null);
    }

    private Meridian() {
    }
}
