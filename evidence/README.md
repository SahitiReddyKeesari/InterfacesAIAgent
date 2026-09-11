# Evidence

One run per outcome, plus the discovery run that produced the capability. Each directory
holds `events.jsonl` (a structured record, appended as it happens), `summary.json`,
`result.json`, and on failure a screenshot. Every string is scrubbed on the way to disk,
so nothing here contains a tax identifier, a card number, or a value declared sensitive.

## The through-line, in two numbers

| | |
|---|---|
| Learning the flow, with a model driving the UI | **1570 s** |
| Replaying it afterwards, no model | **2–3 s** |

Same task. The difference is the model leaving the loop.

## The runs

| Directory | Outcome | What it shows |
|---|---|---|
| `discovery-*` | success | A real LLM-driven run against the live legacy UI. Holds `transcript.json` (what the model saw and decided each turn) and `capability.json` (what it produced). |
| `replay-*` | `success` | The discovered capability replayed with arguments it was never recorded with. |
| `replay-*` | `business_outcome` | A request naming two accounts of the same product — reported as `ambiguous_request`, with what to supply instead. |
| `replay-*` | `recovered` | An injected session timeout, detected and handled; the declared outputs still returned. |
| `replay-*` | `hard_failure` | An injected server error. Stops at the failing step, reports expected against observed, captures a screenshot. |
| `replay-*` | `blocked_by_policy` | Pointed outside its allowlist. Refused before acting; nothing executed. |
| `replay-*` | `escalated` | Stuck, nobody took the intervention. Reports `escalated` rather than pretending it finished. |
| `replay-*` (with `escalations`) | `recovered` | Stuck, a person took the live session, repaired it, chose a resume point, and the run finished. |

Reproduce the last four with `python scripts/escalation_scenarios.py`.

## Two guards firing on a live model

Worth opening `discovery-*/events.jsonl` for these — they are a real model being caught,
not a contrived test:

- **`checkpoint_discarded`** — the model claimed a heading proved a step had worked. That
  heading does not exist in this application. Recorded unverified it would have failed
  every future replay, and the failure would have looked like UI drift rather than a bad
  recording. Checkpoints are verified when recorded and dropped if they did not hold.
- **`derived_scope: {account_type}`** — a grid cell is identified by its column, so it
  needs a row. The row is visible at record time, so the anchor is computed from what was
  perceived and bound to a caller-supplied value. An earlier version anchored a status
  cell to the *balance* beside it — a step that breaks the next time the member spends
  anything.

## Outcomes were derived, not declared

Discovery records the happy path, so the capability it produced could not tell "no such
member" from a broken application. `cua probe` closed that without a model: it replays the
plan twice, once with arguments that succeed and once with arguments chosen to provoke the
condition, and keeps whatever the application said the second time and not the first.

```
signature: ['No member records match the criteria entered.']
```

No model is involved, deliberately — a signature invented by an LLM would be the
hallucinated-checkpoint problem in a new costume.

`ambiguous_request` is different: the application never reports it, because nothing is
wrong from its point of view. The system detects it by finding more than one record behind
one key, and refuses rather than answering about whichever was listed first.
