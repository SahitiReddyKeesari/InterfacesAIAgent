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

    static final class Member {
        final String memberId;
        final String firstName;
        final String lastName;
        final String ssn;   // sensitive
        final String dob;   // sensitive
        final String status;
        final String branch;
        final List<Account> accounts = new ArrayList<>();

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

    static String money(double v) {
        return String.format("%.2f", v);
    }

    static {
        reset();
    }

    private Data() {
    }
}
