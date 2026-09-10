# Evidence

One run per outcome class, plus the discovery run that produced the capability. Each
directory holds `events.jsonl` (a structured, append-as-it-happens record),
`summary.json`, and — on failure — a screenshot. Every string is scrubbed on the way to
disk, so nothing here contains a tax identifier, a card number or a value declared
sensitive.

## The end-to-end thread

| Run | What it shows |
|---|---|
| `discovery-*` | A real LLM-driven run against the live legacy UI. Contains `transcript.json` (what the model saw and decided, turn by turn) and `capability.json` (what it produced). |
| `replay-*` `success` | The **discovered** artifact replayed with arguments it was never recorded with — `member_id=12347`, `account_type=Certificate` — returning `current_balance=25000.00, status=Matured`. |
| `replay-*` `business_outcome` | `member_id=99999` → `member_not_found`. A legitimate answer returned by name, with no error and no screenshot: nothing went wrong. |
| `replay-*` `recovered` | An injected session timeout, detected, recovered from, and the declared outputs still returned. `recoveries: ["session_expired"]`. |
| `replay-*` `hard_failure` | An injected server error. Stops at the failing step, reports what was expected against what was observed, and captures a screenshot. |

The artifact was recorded against `member_id=12345 / Savings` and replays correctly for
`12346/Savings`, `12347/Certificate` and `12345/Checking`. Nothing was re-recorded.

## Worth reading in the discovery log

Two events show guards firing on a live model, not on a contrived test:

- **`checkpoint_discarded`** — the model claimed `"Member Profile"` proved a step had
  worked. That heading does not exist in this application. Recorded unverified it would
  have failed every future replay, and the failure would have looked like UI drift
  rather than a bad recording. Checkpoints are verified at record time and dropped if
  they did not hold.
- **`derived_scope: {account_type}`** — a grid cell is identified by its column, so it
  needs a row. The row is visible at record time, so the anchor is computed from what
  was perceived and preferentially bound to a caller-supplied value. An earlier version
  anchored a Status cell to the *balance* beside it — a step that breaks the next time
  the member spends anything.

## A visible limit

Replaying the **discovered** artifact with `member_id=99999` returns a `hard_failure`,
not `member_not_found`. Discovery records the happy path; known business outcomes are
declared, and this artifact has none. The hand-recorded capability
(`meridian.read_account_balance`) declares them, which is why the same input is a
business outcome there. Deriving them automatically is the first item in `REPORT.md` §7.
