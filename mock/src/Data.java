import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Seed data for the mock back-office.
 *
 * Deliberately carries fields that must never reach an artifact or a log (SSN, full
 * date of birth) so redaction has something real to prove itself against. The UI only
 * ever renders the masked form.
 */
final class Data {

    static final class Account {
        final String number;
        final String kind;
        double balance;
        String status;

        Account(String number, String kind, double balance, String status) {
            this.number = number;
            this.kind = kind;
            this.balance = balance;
            this.status = status;
        }
    }

    /** A payment card. The PAN is sensitive and is never rendered - only the last four. */
    static final class Card {
        final String pan;      // sensitive
        final String kind;     // "Debit" | "Credit"
        final String expiry;
        String status;         // "Inactive" | "Active" | "Locked" | "Blocked"

        Card(String pan, String kind, String expiry, String status) {
            this.pan = pan;
            this.kind = kind;
            this.expiry = expiry;
            this.status = status;
        }

        String last4() {
            return pan.substring(pan.length() - 4);
        }

        String masked() {
            return "**** **** **** " + last4();
        }
    }

    /** Outcome of a card action: a business result, never an exception. */
    record CardResult(boolean ok, String message) {
    }

    static final class Member {
        final String memberId;
        final String firstName;
        final String lastName;
        final String ssn;   // sensitive
        final String dob;   // sensitive
        final String status;
        final String branch;
        final List<Account> accounts = new ArrayList<>();
        final List<Card> cards = new ArrayList<>();

        Member(String memberId, String firstName, String lastName, String ssn, String dob,
               String status, String branch) {
            this.memberId = memberId;
            this.firstName = firstName;
            this.lastName = lastName;
            this.ssn = ssn;
            this.dob = dob;
            this.status = status;
            this.branch = branch;
        }

        String fullName() {
            return firstName + " " + lastName;
        }

        String maskedSsn() {
            return "XXX-XX-" + ssn.substring(ssn.length() - 4);
        }
    }

    private static final Map<String, Member> STATE = new LinkedHashMap<>();

    static synchronized void reset() {
        STATE.clear();
        Member m1 = new Member("12345", "Dolores", "Abernathy", "412-88-9031", "1971-03-14",
                               "Active", "Sweetwater");
        m1.accounts.add(new Account("0001234501", "Savings", 8421.55, "Open"));
        m1.accounts.add(new Account("0001234502", "Checking", 1290.03, "Open"));

        Member m2 = new Member("12346", "Bernard", "Lowe", "501-22-7744", "1969-11-02",
                               "Active", "Escalante");
        m2.accounts.add(new Account("0001234601", "Savings", 152.10, "Open"));

        Member m3 = new Member("12347", "Maeve", "Millay", "377-45-1188", "1980-06-23",
                               "Restricted", "Sweetwater");
        m3.accounts.add(new Account("0001234701", "Checking", 44210.87, "Open"));
        m3.accounts.add(new Account("0001234702", "Certificate", 25000.00, "Matured"));

        Member m4 = new Member("12348", "Teddy", "Flood", "298-13-5560", "1985-01-30",
                               "Dormant", "Las Mudas");
        m4.accounts.add(new Account("0001234801", "Savings", 0.00, "Closed"));

        m1.cards.add(new Card("4539881022444412", "Debit", "11/28", "Active"));
        m1.cards.add(new Card("5412750199308830", "Credit", "04/27", "Inactive"));
        m2.cards.add(new Card("4539881044101177", "Debit", "09/26", "Locked"));
        m3.cards.add(new Card("4539881077029902", "Debit", "02/29", "Active"));
        m4.cards.add(new Card("5412750133445540", "Debit", "07/25", "Blocked"));

        for (Member m : List.of(m1, m2, m3, m4)) {
            STATE.put(m.memberId, m);
        }
    }

    static synchronized Member get(String memberId) {
        return memberId == null ? null : STATE.get(memberId.trim());
    }

    /** Match on member id, or a case-insensitive substring of the name. */
    static synchronized List<Member> search(String term) {
        List<Member> hits = new ArrayList<>();
        if (term == null || term.isBlank()) {
            return hits;
        }
        String t = term.trim().toLowerCase(Locale.ROOT);
        for (Member m : STATE.values()) {
            if (m.memberId.equals(t)
                    || m.fullName().toLowerCase(Locale.ROOT).contains(t)
                    || m.lastName.toLowerCase(Locale.ROOT).contains(t)) {
                hits.add(m);
            }
        }
        return hits;
    }

    static synchronized Account openSubaccount(String memberId, String kind, double initial) {
        Member m = get(memberId);
        if (m == null) {
            return null;
        }
        String number;
        if (m.accounts.isEmpty()) {
            number = memberId + "01";
        } else {
            long last = Long.parseLong(m.accounts.get(m.accounts.size() - 1).number);
            number = String.format("%010d", last + 1);
        }
        Account a = new Account(number, kind, initial, "Open");
        m.accounts.add(a);
        return a;
    }

    static synchronized Card findCard(String memberId, String last4) {
        Member m = get(memberId);
        if (m == null) {
            return null;
        }
        for (Card c : m.cards) {
            if (c.last4().equals(last4)) {
                return c;
            }
        }
        return null;
    }

    /**
     * Apply a card action.
     *
     * Every rejection here is a business outcome the caller needs to know about, not a
     * crash: a card in the wrong state, a card that does not exist, or an operator who
     * is not permitted to service a restricted relationship. Only "block" is
     * irreversible - there is deliberately no path back out of Blocked.
     */
    static synchronized CardResult cardAction(String memberId, String last4, String action) {
        Member m = get(memberId);
        if (m == null) {
            return new CardResult(false, "Member context lost.");
        }
        if (m.status.equals("Restricted")) {
            return new CardResult(false,
                    "Operator not authorized for restricted relationship (SEC-0917).");
        }
        Card c = findCard(memberId, last4);
        if (c == null) {
            return new CardResult(false, "Card not found for this relationship (CRD-2199).");
        }
        switch (action) {
            case "activate" -> {
                if (!c.status.equals("Inactive")) {
                    return new CardResult(false,
                            "Card is not awaiting activation (CRD-2201).");
                }
                c.status = "Active";
                return new CardResult(true, "Card " + c.masked() + " activated.");
            }
            case "lock" -> {
                if (!c.status.equals("Active")) {
                    return new CardResult(false,
                            "Only an active card can be locked (CRD-2203).");
                }
                c.status = "Locked";
                return new CardResult(true, "Card " + c.masked() + " locked.");
            }
            case "unlock" -> {
                if (!c.status.equals("Locked")) {
                    return new CardResult(false,
                            "Only a locked card can be unlocked (CRD-2204).");
                }
                c.status = "Active";
                return new CardResult(true, "Card " + c.masked() + " unlocked.");
            }
            case "block" -> {
                if (c.status.equals("Blocked")) {
                    return new CardResult(false, "Card is already blocked (CRD-2210).");
                }
                c.status = "Blocked";
                return new CardResult(true,
                        "Card " + c.masked() + " permanently blocked.");
            }
            default -> {
                return new CardResult(false, "Unsupported card action.");
            }
        }
    }

    static String money(double v) {
        return String.format("%.2f", v);
    }

    static {
        reset();
    }

    private Data() {
    }
}
