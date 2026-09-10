# Design write-up

## 1. Architecture

The system is four layers with a strictly one-way dependency graph:

```
discovery/  (a model runs here, once)          replay/  (no model, ever)
        \                                        /
         \-------- artifact/  (the contract) ---/
                          |
                    surfaces/  (perceive + act)
                          |
        safety/ and escalation/ wrap a surface as decorators
```

**The seam is `surfaces/`.** Everything above it speaks only `Observation`, `Action`,
`Locator` and `Checkpoint`. Only `web.py` knows a DOM exists. This is the answer to the
brief's question about the boundary between "how we perceive and act on a surface" and
"the recorded flow": a recorded step says *fill the control captioned "Member / Name:"
with `{member_id}`*, which is a claim about a user interface, not about a browser.

**`replay/` imports nothing from `discovery/`.** The claim "no model on the production
path" is therefore a property of the dependency graph rather than a promise — the code
that knows how to consult a model is not reachable from the code that executes in
production. A test asserts it.

**Policy and control transfer are decorators, not calls.** `PolicySurface` and
`ControlledSurface` implement `Surface` and wrap another one, so every action reaches the
real surface through them. A guardrail the caller has to remember to consult is not a
guardrail: an allowlist violation is impossible regardless of what the discovery loop
believes it is doing. They compose as `Controlled(Policy(Playwright))` — a human may act
freely during a handoff, while every *automated* action underneath still passes the
allowlist.

**Trade-off taken:** a single process, synchronous, no queues or services. The brief is
explicit that building scaling infrastructure is not rewarded, and the interesting
problems here are the contract and the failure taxonomy, not the transport. The one place
that would obviously become a service — the intervention queue — is already behind a
four-method interface backed by a directory of JSON files.

## 2. Artifact schema

The artifact is a **capability contract**, not a step list, because it has two readers: a
human approving it for unattended use, and an agent deciding whether to call it. It
declares typed inputs, typed outputs, an ordered plan, a success checkpoint, known
business outcomes, recoverable conditions, provenance and an approval state.

Three commitments shaped it.

**Values never appear in the plan — only placeholders.** A recorded step fills
`{member_id}`, never `12345`. Parameterisation happens at record time: the caller names
which values are arguments and supplies an example, and wherever the model types or
matches one, the artifact records the placeholder. That is what lets one recording serve
every member, and it doubles as redaction — a saved artifact physically cannot contain the
data of whoever it was recorded against. A sensitive input additionally carries no
`example`, because an example of a tax identifier is a tax identifier.

**Success is not the only declared ending.** A capability that knows only what success
looks like must treat "no such member" as a failure — which is wrong, and which the brief
names as the most common mistake in this problem. So the artifact declares three kinds of
ending with recognisable signatures: the success checkpoint, *known business outcomes*
(returned to the caller by name), and *recoverable conditions* (with a recorded recovery
sequence — not model reasoning, which would put a model back in the production path).
Anything matching none of them is a hard failure.

**Robustness reasoning is recorded, not just the selector.** Every locator carries why it
identifies a control that way, and those rationales appear in `cua show`. A reviewer
approving unattended execution can see that a step is anchored to a caption rather than to
a row index. `validate_contract()` refuses to approve an irreversible plan with no declared
outcomes.

Versioning is content-driven: saving a plan whose *executable* content differs writes a new
version rather than overwriting, so a capability an agent is already calling cannot change
underneath it. The fingerprint deliberately excludes prose — rewording a rationale must not
force re-approval, because forcing re-approval for a typo trains reviewers to rubber-stamp
version bumps.

## 3. Determinism & error handling

**Locators are a fallback chain, ordered by durability**, each candidate carrying a
confidence and a rationale: `role_name` (0.92) → `label_text` (0.85) → `column_cell`
(0.88 for grid cells) → `control_id` (0.70, or 0.35 when positional) → `ordinal` (0.15).
No CSS selector appears anywhere — it is the one vocabulary with no equivalent off a
browser.

Two findings drove the design:

- **A field's caption is often its only handle.** The Member/Name input has no accessible
  name, no `<label for>`, no title. `label_text` — the text in the adjacent cell — resolves
  it. That has a direct desktop analogue (the static text preceding a control).
- **Resolving is not the same as being right.** An unscoped "Select" link falls through to
  a row-indexed id, matches *exactly one* control, and confidently clicks the wrong member.
  So `Resolution` carries the winning confidence and a `weak` flag, `ReplayResult` reports
  `weak_steps`, and grid targeting is scoped by **business key** — *the row containing
  `{member_id}`* — rather than by position. Positional ids are detected structurally (peers
  of the same role whose ids match once digits are collapsed), not by pattern-matching one
  framework's conventions, so ASP.NET's `ctl02`, Struts' `row_3` and JSF's `tbl:0:btn` are
  all caught.

**Waiting is adaptive, never fixed.** A postback navigates an inner frame while the top
document stays put, so page load state returns immediately. Content stability alone is also
insufficient: while a slow request is still in flight *nothing has changed yet*, so a text
fingerprint looks perfectly quiet and the stale page gets read as the new one. Replay
therefore waits for in-flight document requests to return, then for rendered content to
stop moving. Against a backend stalled for six seconds this takes 7.0s; with no stall, 1.1s.

**The result contract separates the three classes** the brief insists on. `ReplayResult`
carries `outcome` (`success` / `business_outcome` / `recovered` / `hard_failure` /
`blocked_by_policy` / `escalated`), the declared outputs on success, a *named* business
outcome, the recoveries used, and on failure the step, what was expected and what was
observed. Classification happens *before* despairing of a step, because these conditions
usually present as a step failing — when a search returns nothing, the row to click simply
is not there.

## 4. Heterogeneity & multi-tenant

**Extending to other surfaces.** The locator vocabulary was chosen for portability: role,
accessible name, caption text, column heading and ordinal all exist in desktop
accessibility APIs (AX, UIA) as well as in a browser. Adding a surface means adding one
`Surface` implementation; the artifact and the replay engine do not change. The evidence
that this is real rather than asserted: the same engine drives two mechanically unrelated
dialects — a frameset with `__doPostBack` and `__VIEWSTATE`, and an iframe workspace with
Struts `.do` actions and a synchronizer token — with no branch on which one it is, and it
also handles modern semantic markup it was never designed for, degrading gracefully to
`role_name` when real labels exist.

**The honest limit:** one surface is implemented. A 3270 green screen is a different
*modality*, not uglier HTML — it has no element tree, so perception becomes "parse an 80×24
grid into fields". It fits `Observation`/`Action`, but that is an argument, not a
demonstration. `frame_path` is also the seam's least clean part; a desktop surface would
reinterpret it as a window/pane path.

**Multi-tenant reuse.** `SurfaceBinding` carries `tenant` and `variant` fields, unused
today and reserved deliberately. The intended model is a **base capability per vendor
product plus per-tenant overrides** — a tenant record supplying an entry URL and a sparse
set of locator or checkpoint overrides keyed by step, rather than a re-recording per
institution. Two mechanisms already present make that workable: the fingerprint identifies
whether two tenants are running the same plan, and the `weak_steps` signal gives per-tenant
drift detection — a step that starts resolving via a fallback on one tenant is exactly the
signal that that tenant's variant needs an override. Terminology drift is the common case
and is why the mock renames every concept between dashboards (member→customer,
sub-account→related account): a strategy that memorised one tenant's wording breaks on the
next, which is a bug you want to find in a test rather than in production.

## 5. Escalation & handoff

**Detecting stuck** is the same classifier as the error taxonomy: a step that did not
complete, that matches no declared business outcome and no recoverable condition, has by
definition run out of recorded knowledge. That is the moment to ask a person.

**Transferring control.** `ControlledSurface` wraps the live surface and holds a `Control`
state. While a human holds it, automation actions are *refused* — not discouraged — so a
stray retry cannot fight the operator for the keyboard mid-transaction. The intervention
request carries what an operator needs to act: capability, goal, step index and intent,
what was expected, what was observed, the URL, and a screenshot. The session handed over is
the one the run was already driving, not a fresh one.

**Handing back.** The operator releases through the broker and control returns to
automation. Two details matter more than the queue. First, **the operator chooses the
resume point**: retrying only the failed step is the default, but someone who reset the
application has invalidated the earlier steps too, so `resume_from_step` lets them say
where it is safe to pick up. Second, **what the human did is recorded** — observations are
diffed either side of the handoff and the changes land in the run's evidence, because the
automation resumes on a session someone else changed.

**The seam an operator console plugs into** is `operator_adapter`: something that receives
the live surface and the request and performs the work. In production that is a person at a
headed browser and no adapter is present. The same seam takes an operator console driving
the session on a person's behalf, or a scripted remediation for a condition a team has
chosen to automate.

**Mocked deliberately:** real-time co-browsing is out of scope per the brief. There are
two operator surfaces, both thin: a CLI (`cua operator list/take/release`) and a browser
console (`cua console`) that lists waiting interventions with their context and lets a
person take control and hand it back. Neither streams the session - the human works the
headed browser the run is already driving. The control-transfer protocol, the
single-holder guarantee, the resume signal and the change record are all real.

The console needs no Node and no build step: React is vendored and the server is the
standard library. That was a deliberate constraint - a repo whose only prerequisites are
Python and a JDK should not acquire a JavaScript toolchain for an optional view.

## 6. Safety

**A default-deny allowlist enforced at the seam.** Empty means "nothing permitted", never
"no restriction" — a policy that opens up when misconfigured is worse than none, because it
reads like protection. Matching is on scheme, host and path, never substring, so
`127.0.0.1:8080.evil.test` cannot pass a rule written for `127.0.0.1:8080`. Because it is
enforced by the surface wrapper, the check also catches navigation the *page* initiates:
after every action the landing URL is re-checked, so a click that carries the session
somewhere forbidden is caught, not just a navigation the automation requested.

**Risk is classified per step and gated separately**, because the surface sees a click and
cannot know what it means, while the engine sees intent. `irreversible` steps require
explicit approval and the default approver refuses — unattended execution must not silently
self-approve. The model classifies each action's consequence during discovery and that
classification is recorded in the artifact for review.

**Redaction has two layers.** Declared secrets are exact but only cover what someone
remembered to declare; shape detection (tax identifiers, and card numbers confirmed by
Luhn so reference numbers are not mangled) catches what nobody declared. Both are applied
on the way *out* of the surface and again on the way into evidence, so anything written
down is already scrubbed. A fill of a sensitive value registers that value with the
redactor as it happens.

**Limits, stated plainly.** Screenshots cannot be scrubbed after the fact — a rendered tax
id is pixels — so the policy's answer to a sensitive screen is not to capture it rather
than to mask it. Shape detection cannot be complete. And the allowlist constrains *where*
the automation acts, not *what it means to do* there; a capability approved to service
accounts can still service the wrong account if its locators are weak, which is why weak
resolution is reported rather than silently tolerated.

## 7. Cuts

**Left out deliberately:**

- **A real operator console.** Out of scope per the brief; the CLI stands in and the seam
  it plugs into is real.
- **Tomcat and real JSPs for the mock.** The emitted HTML — the only thing the automation
  observes — is identical either way, and a container adds a download, a deploy step and a
  second failure mode for the reviewer with no gain in what is being tested.
- **Multi-tenant and desktop implementations.** Designed for, not built, as the brief
  allows. `tenant`/`variant` are reserved so the abstractions are not cornered.
- **Discovery of business outcomes.** The recorded run captures the happy path; known
  outcomes and recoverable conditions are declared. They could be *derived* without a model
  by replaying the recorded plan with a deliberately bad argument and capturing the
  divergent text as a signature — a cheap, obvious next step I ran out of time for.
- **Queues, workers, retries across processes.** Explicitly not rewarded.

**Built after the first pass, because the gap was visible in the evidence:**

**Outcome probing** (`cua probe`). A discovered artifact knew only the happy path, so an
unknown member came back as a hard failure rather than as `member_not_found`. Probing
replays the plan twice - once with arguments that succeed, once with arguments chosen to
provoke the condition - and records whatever the application said the second time and
not the first. Candidates come from perceived elements rather than flattened page text,
so a message arrives as one string; they are then ranked so that the application *saying*
something (a sentence, or better a condition code like `SEC-0917`) outranks a row count
or a field caption that merely happened to differ. No model is involved, deliberately: a
signature invented by an LLM is the hallucinated-checkpoint failure in a new costume.

**What I would build next, in order:**

1. **Per-tenant overrides**, since the schema already reserves the fields and `weak_steps`
   already provides the drift signal that should trigger creating one.
2. **A second surface implementation** — a desktop app via the macOS AX API. Everything
   about §4 is an argument until that exists, and it is the fastest way to find out which
   parts of the seam are honest.
3. **Multi-run stability scoring**, replaying N times and gating unattended approval on it;
   the approval state and the weak-step signal are both already in place to hang it on.

**What I would change with hindsight:** `frame_path` should have been a generic
`container_path` from the start. It is the one place a browser concept leaked into a
vocabulary that is otherwise surface-agnostic, and it will need renaming the moment a
desktop surface exists.
