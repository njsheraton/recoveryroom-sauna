# Recovery Room sauna controller: plan, inputs and gates

Prepared from the handoff *Recovery Room Sauna Automation Handoff* (spec v1.0, 29 Oct 2026).
Status date: 28 Oct 2026. Build: v0.1.0, read-only.

This is the "first report" the handoff's Section 14 asks for: proposed architecture, missing
inputs, compatibility evidence, security boundaries and safety-approval requirements. It is a
software plan, not electrical, medical or commercial safety certification.

---

## 1. What exists after this build

| Area | Status | Where |
|---|---|---|
| Provisional LAN protocol (framing, varint, state decode, control payload builders, confirmation predicates) | Built, fixture-tested | `recoveryroom_sauna/protocol.py` |
| Deterministic simulated sauna with fault injection | Built, tested | `recoveryroom_sauna/simulator.py` |
| Read-only LAN worker (login, heartbeat, poll, freshness, bounded reconnect) | Built, tested against simulator | `recoveryroom_sauna/device.py` |
| Operating-hours policy (weekly hours, overrides, 30/30 margins, deadline and bounded-timer maths) | Built, fake-clock tested | `recoveryroom_sauna/business_hours.py` |
| Versioned config contract where every null blocks heating | Built, tested | `recoveryroom_sauna/config.py`, `config/example.config.json` |
| Owner-local PIN pairing, hashed sessions, atomic 0600 private files, fail-closed on corruption | Built, tested | `recoveryroom_sauna/security.py` |
| Status-only dashboard on 127.0.0.1 with Host/Origin/CSP/body checks | Built, tested | `recoveryroom_sauna/server.py`, `static/` |
| Read-only LAN diagnostic to run on the shop Mac | Built | `tools/lan_diagnose.py` |
| Control writes of any kind | **Not built.** `send_control()` refuses. No endpoint can change device state. | |
| Automation supervisor, arming, durable Stop, renewals, handover | **Not built** (phase 3–4) | |
| Installer, launchd agent, rollback, staff guide | **Not built** (phase 5–6) | |

Test run on 28 Oct 2026: 141 passed (see README for the command). These are simulated tests.
They demonstrate software behaviour only. Nothing has touched a real sauna.

---

## 2. Proposed architecture

```
Staff browser on shop Mac  (http://127.0.0.1:8787, paired once with an owner PIN)
   -> recoveryroom_sauna.server     authenticated status + (later) control API, strict Host/Origin/CSP
   -> AppContext                    read-only view of device snapshot + hours policy
   -> device.DeviceThread           one asyncio loop, one persistent TCP session, freshness tracking
   -> Clearlight sauna              TCP 12416 on the shop LAN (DHCP reserved, identity verified)

Phase 3 adds:  automation.CommandWorker   one serialised appliance-command worker; durable priority Stop
Phase 4 adds:  business_hours.Supervisor  daily arming, dispatch-at-most-once, fixed closing cutoff
Optional:      cloud fallback (Gizwits), Cloudflare tunnel. Both disabled, both need separate approval.
```

Design rules carried through every layer:

- **Power, freshness, automation mode and schedule eligibility are separate fields.** An open socket
  is not health. A fresh report is a 0x0091 payload decoded without error inside the 20-second limit.
- **Unknown stays unknown.** Stale or disconnected never renders as OFF.
- **Nulls block.** Weekly hours, continuous-operation limit, device address, writes flag, inspection
  approval and manufacturer approval must all be set before scheduled heating can be enabled.
- **No LLM at runtime.** Pure Python 3.11+ standard library, no third-party runtime dependency.
- **Loopback only.** The backend refuses to bind anything but 127.0.0.1 / ::1.

---

## 3. Phased build and the gate at each step

| Phase | Deliverable | Gate before next phase |
|---|---|---|
| 1 (done) | Project, config contract, protocol fixtures, simulator | Tests pass |
| 2 (done, pending field data) | Read-only LAN worker, status-only UI, diagnostic tool | **Field gate A:** diagnostic run on the shop Mac shows stable, fresh reports that match the physical panel |
| 3 | Serialised command worker, intent persistence, bounded ACK/readback waits, outcome classification (rejected / confirmed / uncertain), durable priority Stop with retry, restart recovery to pending Stop | Offline fault tests pass; **Field gate B:** supervised one-change-at-a-time commissioning with nobody in the cabin |
| 4 | Business-hours supervisor: daily arming, at-most-once dispatch, fixed closing cutoff across every mode, manual stop semantics, closed-today, owner/staff roles, hours editing with revisions | Fake-clock multi-day tests; policy review with John |
| 5 | Installer with off-state gates and lock files, launchd LaunchAgent, rollback, version/build verification | Install on the shop Mac with writes disabled, then enable manual commands only |
| 6 | Supervised pilot, staff quick guide, known-limitations register, scheduled preheat enabled on one approved day | John + manufacturer/service representative sign-off on the operating procedure |

Cloud fallback and remote HTTPS are commissioned separately and only if wanted.

---

## 4. Inputs needed from John

Items marked **blocks A** are needed before the read-only field test; **blocks B** before any write;
**blocks C** before scheduled preheat. Everything else can wait.

### Equipment and manufacturer (safety)
1. **Exact sauna model and controller.** Model confirmed by John on 29 Oct 2026: **Clearlight
   Sanctuary 3**. Still needed: which phone app controls it. Clearlight ships two incompatible
   systems: the older *Clearlight Sauna Connect* (2.4 GHz only) and the newer *Clearlight Sauna
   Connect 5GHz* (Bluetooth-assisted setup, needs a 5 GHz-compatible power supply). The handoff's
   reference protocol was observed on one of these; if the shop unit runs the other, the LAN
   protocol may be entirely different. A photo of the controller label and the app's device-info
   screen settles it. *(blocks A)*
2. **Current manual** for that model, and the printed page that covers remote operation.
   The ANZ 2022 Sanctuary manual (p.96 item 19) requires cabin inspection before a remote start or
   timer restart. *(blocks B)*
3. **Written answer from Clearlight / the distributor** on three points: remote start is acceptable
   for this installation; repeated 30-minute timer renewals are acceptable; the maximum continuous
   run time they will approve for commercial use. Contact name and date. *(blocks B for the first two,
   blocks C for the limit)*
4. **Approved maximum continuous run** in minutes, from item 3. This becomes
   `continuousOperationLimitMinutes`. Without it the controller keeps a conservative independent limit
   and scheduling stays off. *(blocks C)*
5. **Warm-up target and ready threshold.** Reference values are 80 °C target and 55 °C ready;
   confirm or change. *(blocks B)*

### Operating hours and procedure
6. **Weekly hours**: open and close per weekday, Brisbane time, or CLOSED. Overnight hours are not
   supported in version one. *(blocks C)*
7. **Closures**: public holidays observed, any planned closures or special-hours dates for the
   next 12 months. *(blocks C)*
8. **Confirm the margins**: preheat 30 minutes before opening, latest shutdown 30 minutes after
   closing. Both are configurable. *(blocks C)*
9. **Daily inspection and arming**: who inspects the cabin each morning, what the checklist is,
   and who is allowed to "arm" the day in the dashboard. A written one-page procedure is enough.
   *(blocks C)*

### Shop Mac and network
10. **The Mac**: model, macOS version, whether it is wired or on Wi-Fi, the staff account name,
    whether FileVault is on, and whether it stays logged in after a reboot. The service runs as a
    per-user LaunchAgent, so it only runs while that account is logged in. *(blocks 5)*
11. **Sauna network identity**: the sauna's private IP, its MAC address, and confirmation that
    the router has a DHCP reservation for it. Also whether guests share that Wi-Fi (recommend a
    separate guest network). *(blocks A)*
12. **Same-Mac access is enough?** Recommended answer: yes for version one. No tunnel, no public
    hostname, no port forwarding. If remote viewing is wanted later it is a separate approved change.

### Accounts and access
13. **A private Git repository** (GitHub is fine) that I can push to. This cloud session is
    ephemeral; the code must live somewhere durable. Until then I will hand over zip archives.
14. **Owner identity** for the audit trail (name and email) and the list of staff who will use the
    shared browser. Sessions authenticate the workstation, not individuals; owner-only actions
    (hours, limits, revocation) will be separated in phase 4.
15. **Cloud fallback**: recommend *not* provisioning anything now. It needs an authorised Gizwits
    binding for this exact unit; if Clearlight cannot provide a legitimate route, we stop at LAN-only.
16. **Cloudflare**: not needed. Only create an account if you decide you want remote access later.

### Commissioning windows
17. **A read-only test slot** (about 30 minutes) when you can run `tools/lan_diagnose.py` on the shop
    Mac while watching the physical panel. *(gate A)*
18. **A supervised write-commissioning slot** later (about 90 minutes, cabin empty, you present),
    after Clearlight's answer. *(gate B)*

---

## 5. Compatibility evidence

Honest position: **none yet for this unit.** The protocol in `protocol.py` is transcribed from the
handoff's description of a working home installation of the same brand. Same brand or model does not
establish identical firmware or protocol behaviour. Evidence will come from gate A:

- the diagnostic connects, obtains a passcode, logs in (0x0009 first byte 00)
- 0x0091 reports arrive at least every ~5 s, decode without error, and payload length is stable
- the decoded power, lights, target and timer match the physical panel through several minutes
- toggling a light at the physical panel is reflected in the next report
- the `rawHex` field shows which of the "preserved unknown" bytes move, so we do not guess at them

Only after that do we discuss any control frame, and then one at a time under supervision.

---

## 6. Security boundaries

- **Network**: backend bound to loopback only (enforced in config validation). No port forwarding,
  no tunnel by default. Sauna port is never proxied. The LAN is still a trust boundary; guest Wi-Fi
  should be separate from the sauna and the Mac.
- **Browser access**: one-time 6-digit PIN created by an owner-local CLI command, 10-minute life,
  5 attempts, single use. Session token is 32 random bytes, stored only as a SHA-256 hash, compared in
  constant time, delivered as an HttpOnly SameSite=Strict cookie. Default session life 30 days
  (configurable; shorter than the home one-year default).
- **Request hygiene**: exact Host allow-list, Origin check on every POST, no CORS, CSP
  `default-src 'none'`, JSON-only bodies up to 4 KB, per-process rate limits, no raw-command endpoint,
  no arbitrary URL proxying.
- **Private data**: `private/` directory 0700, files 0600, atomic writes with fsync. Corrupt or
  world-readable security files stop the service rather than being recreated. The LAN passcode is
  used in memory for login only and is never logged or stored.
- **Git**: `.gitignore` excludes `private/`, state, logs, backups and `BUILD_ID`. Example config
  contains no secrets.
- **Health endpoint**: unauthenticated, returns only version and build.

---

## 7. Safety approvals required before each capability

| Capability | Needs |
|---|---|
| Read-only status (this build) | Confirmed sauna address; your OK to run the diagnostic |
| Any control write (lights, target, timer, power) | Clearlight's written answer on remote start; confirmed model/manual; supervised slot; `commissioning.writesEnabled=true` set by you |
| Manual warm-up / handover | Above, plus an approved continuous-operation limit |
| Scheduled preheat | Above, plus confirmed hours and closures, written inspection and arming procedure, `autoStartEnabled=true`, and one explicitly approved first day |
| Cloud fallback | Authorised Gizwits binding for this unit; separate commissioning including timed-out-command uncertainty |
| Remote HTTPS | Your explicit approval of internet reachability; HTTPS; proxy-header trust limited to the loopback connector |

The software cannot guarantee shutdown if the sauna, Mac, network or command queue fails. The
hardware timer and the manufacturer's thermal protections stay in place, and staff still verify
heating and lights OFF physically at closing.
