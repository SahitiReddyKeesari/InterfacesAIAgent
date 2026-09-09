# Mock back-office

Two stand-in legacy dashboards for the automation system to drive. Pure Java on the JDK's
built-in HTTP server — no Maven, no Gradle, no Tomcat, no third-party jars.

| Path | Dashboard | Dialect |
|---|---|---|
| `/meridian/` | Meridian Core Servicing | ASP.NET WebForms — frameset, `__doPostBack`, `__VIEWSTATE` |
| `/summit/` | Summit Servicing | Java/Struts — `*.do` actions, synchronizer token, iframe workspace |

Both expose the same business flow (search → detail → open an account → confirmation)
behind different markup, navigation mechanics and terminology. Neither has a single test
id, and neither associates a `<label>` with its inputs — a field's only clue is the text
in the adjacent table cell. That is deliberate: it is what makes CSS-selector automation a
dead end and forces a locator strategy that would also survive a desktop surface.

## Run

```
bash mock/run.sh
```

Serves on `http://127.0.0.1:8080` (override with `MOCKBANK_HOST` / `MOCKBANK_PORT`).

## JDK

Needs a JDK 17 or newer. If you don't have one, this installs Temurin 21 into your home
directory with no admin rights and nothing in system paths:

```
mkdir -p ~/.jdks && curl -sL "$(curl -s 'https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=aarch64&image_type=jdk&os=mac&vendor=eclipse' | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["binary"]["package"]["link"])')" | tar -xz -C ~/.jdks
```

`run.sh` finds a JDK on `PATH`, in `JAVA_HOME`, or under `~/.jdks`.

## Card Services

`/meridian/cards.aspx` (nav: **Card Services**). Retrieve a member's cards, then act on one.

| Action | From state | Reversible | Behaviour |
|---|---|---|---|
| Activate | `Inactive` | yes | applies immediately |
| Lock | `Active` | yes | applies immediately |
| Unlock | `Locked` | yes | applies immediately |
| **Block** | any except `Blocked` | **no** | routes to a confirmation screen first |

**Block is deliberately the risky action.** It is irreversible, there is no path back out
of `Blocked`, and it is the only action gated behind an explicit confirmation step - the
grid link opens a warning screen rather than firing the change. A blocked card offers no
further actions in the UI, and forging the postback anyway returns a business outcome
(`CRD-2210`), not a crash. That gives the automation a concrete risky/irreversible
operation to classify and handle conservatively.

Wrong-state and authorisation rejections are business outcomes with codes, not failures:

| Code | Meaning |
|---|---|
| `CRD-2201` | card is not awaiting activation |
| `CRD-2203` | only an active card can be locked |
| `CRD-2204` | only a locked card can be unlocked |
| `CRD-2210` | card is already blocked |
| `CRD-2199` | card not found on this relationship |
| `SEC-0917` | operator not authorised for a restricted relationship |

`SEC-0917` fires for member `12347` (Restricted), which is where the `permission_denied`
class of the fault taxonomy shows up in real data rather than only as an injected switch.

Card PANs are stored in full and **never rendered** - the UI shows `**** **** **** 4412`
only, the same redaction surface as the member tax id.

### Seed cards

| Member | Card | Type | Status |
|---|---|---|---|
| 12345 Abernathy | `****4412` | Debit | Active |
| 12345 Abernathy | `****8830` | Credit | Inactive |
| 12346 Lowe | `****1177` | Debit | Locked |
| 12347 Millay | `****9902` | Debit | Active *(restricted member)* |
| 12348 Flood | `****5540` | Debit | Blocked *(terminal)* |

## Fault injection

Real demo sites will not produce runtime failures on demand, so the target exposes them as
switches. Each fault is tagged with the class a replay must report it as.

```
curl -XPOST localhost:8080/__control/fault -d '{"name":"not_found","count":1}'
curl -XPOST localhost:8080/__control/reset
curl localhost:8080/__control/state
```

| Class | Faults | Replay must |
|---|---|---|
| `business` | `not_found`, `permission_denied`, `validation_error` | return it as a legitimate outcome, not a crash |
| `recover` | `interstitial`, `slow_load`, `session_timeout` | handle it and continue |
| `hard` | `server_error` | stop with a debuggable error |

`slow_load` stalls for 6s specifically so a fixed-duration wait would flake against it.

## Note

A production deployment of this class of app would be a WAR on Tomcat or WebSphere with
real JSPs. That was deliberately cut: the emitted HTML — the only thing the automation
observes — is identical either way, and a container adds a download, a deploy step and a
second failure mode for no gain in what is being tested.
