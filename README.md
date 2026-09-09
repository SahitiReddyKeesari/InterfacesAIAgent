# Computer-Use Automation System

An LLM figures out how to do a task in a legacy UI once. What it learned is frozen into a
typed, versioned **capability artifact**. From then on a deterministic replayer executes
that artifact — no model in the loop — with typed inputs, typed outputs, an explicit error
taxonomy, allowlist enforcement, and a path to hand the live session to a human when it
gets stuck.

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is
> how an agent invokes it in production.

## Prerequisites

| Need | Why |
|---|---|
| **Python 3.11+** | the automation system |
| **JDK 17+** | the mock target application is Java (see [`mock/README.md`](mock/README.md)) |
| **A Gemini API key** | discovery only — replay never calls a model |

No Homebrew, Maven, Gradle, Tomcat or Docker required.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env      # then put your key in GEMINI_API_KEY
```

No JDK? This installs one into your home directory, no admin rights, nothing in system
paths:

```bash
mkdir -p ~/.jdks && curl -sL "$(curl -s 'https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=aarch64&image_type=jdk&os=mac&vendor=eclipse' | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["binary"]["package"]["link"])')" | tar -xz -C ~/.jdks
```

Get a free key at <https://aistudio.google.com/apikey>. `gemini-2.5-flash` is closed to new
keys; the default `gemini-flash-latest` works, and the provider falls back through a list
and retries on the free tier's frequent `503`s.

## Demo path

**1. Start the target application** (leave it running):

```bash
bash mock/run.sh
```

Two deliberately legacy dashboards on `http://127.0.0.1:8080` — an ASP.NET WebForms
dialect at `/meridian/` and a Java/Struts dialect at `/summit/`. Neither has a test id;
neither associates a `<label>` with its inputs.

**2. Discover** — one real LLM-driven run against the live UI:

```bash
cua discover \
  --goal "Look up member 12345 and report the balance and status of their Savings account" \
  --url  "http://127.0.0.1:8080/meridian/" \
  --id   "meridian.read_savings_balance" \
  --param "member_id=12345" \
  --param "account_type=Savings" \
  --describe "member_id=The member number to look up."
```

Writes a capability to `artifacts/` and a full run record to `evidence/`.

**3. Review the contract** a human would approve:

```bash
cua show meridian.read_savings_balance
```

**4. Replay it deterministically**, with different arguments than it was recorded with:

```bash
cua replay meridian.read_savings_balance --arg member_id=12347 --arg account_type=Certificate
```

**5. Replay into an error** — a business outcome, reported as an answer, not a crash:

```bash
cua replay meridian.read_savings_balance --arg member_id=99999 --arg account_type=Savings
```

```bash
curl -XPOST localhost:8080/__control/fault -d '{"name":"server_error"}'   # a hard failure
curl -XPOST localhost:8080/__control/fault -d '{"name":"session_timeout"}' # recovered automatically
```

**6. See it as a calling agent would** — JSON Schema for tool-calling:

```bash
cua catalog
```

## Running without a model

Everything except `cua discover` is model-free. A pre-recorded artifact is committed, so
replay, the outcome taxonomy, safety and escalation can all be exercised with no API key:

```bash
cua replay meridian.read_account_balance --arg member_id=12345 --arg account_type=Savings
```

The test suite likewise needs no key — it starts the Java app itself and uses a scripted
provider in place of a model:

```bash
pytest -q
```

## Human escalation

When replay hits something it cannot explain, it can hand the live session to a person and
resume on that same session afterwards:

```bash
cua replay meridian.read_savings_balance --arg member_id=12345 --arg account_type=Savings \
    --headed --escalate
```

In another terminal:

```bash
cua operator list
cua operator take iv-xxxxxxxx --as mthompson
# do the manual steps in the browser window the run is already using
cua operator release iv-xxxxxxxx --notes "cleared the interstitial"
```

## Layout

```
src/cua/
  surfaces/     the perceive/act seam — Observation, Action, Locator. Knows no application.
  artifact/     the capability contract: schema, argument binding, versioned store
  discovery/    the LLM loop (the only place a model runs) and its providers
  replay/       deterministic execution and the outcome taxonomy
  safety/       allowlist policy, redaction, and the enforcing surface wrapper
  escalation/   control transfer, the intervention queue, the operator seam
  evidence/     structured run records
mock/           the target application (Java, no framework) — a declared stand-in
artifacts/      saved capabilities
evidence/       run records: discovery and replay
```

Design decisions, trade-offs and what was deliberately cut: [`REPORT.md`](REPORT.md).
