# Unverified behaviours and known limitations

Living register. Nothing is removed from here without recorded evidence (what was observed, when,
by whom, on which firmware). Simulated test passes never count as verification of the appliance.

## Protocol (provisional until gate A)
- **Which Clearlight control system the shop Sanctuary 3 uses** (legacy 2.4 GHz app vs the 5GHz app
  generation). Confirmed 29 Oct 2026: legacy *Clearlight Sauna Connect* app, which is the
  generation the reference protocol came from. Still unverified on this unit.
- Frame prefix, varint length, reserved byte and command numbers are transcribed from the handoff,
  not from a manufacturer document.
- Whether 0x0090 (state request) carries a sequence number. This build assumes only 0x0093/0x0094 do.
  If the unit never answers 0x0090, this is the first thing to revisit.
- Whether the unit pushes unsolicited 0x0091 reports, and how often. Diagnostic will show it.
- State payload length. We require >=13 bytes and preserve extras; the real length is unknown.
- Meaning of bytes 0, 2, 3, 4 (reserved, LED, right, left), serial byte 10 and heart byte 12.
- Whether temperatures are always Fahrenheit regardless of the display flag on this firmware.
- Passcode response wrapper format (two-byte length prefix) and passcode lifetime.
- Heartbeat tolerance: how long the unit keeps a session without pings.
- Behaviour when a second LAN client (the vendor phone app) connects at the same time.
- All control payload layouts, ACK format and confirmation timing. **Not exercised at all.**

## Appliance behaviour
- Actual heating rate of the shop cabin from cold; whether 30 minutes of preheat is enough.
- Hardware timer maximum (assumed 60 minutes) and what happens at expiry.
- Whether the keypad can start heating independently while the Mac is connected (assumed yes).
- Thermal cut-outs and any built-in continuous-run limit.
- What the unit reports immediately after power loss and restoration.

## Platform
- Shop Mac sleep, lid, login, FileVault and power-failure behaviour for a LaunchAgent.
- Wi-Fi stability between the Mac and the sauna module over a full business day.
- Clock behaviour on the Mac (NTP) and across DST (Brisbane has none, but the Mac's own zone must be checked).

## Software limitations in this build
- No control path. No automation. No Stop. Physical panel is the only control.
- Hours are displayed from config but nothing acts on them.
- Single shared browser session model; no per-employee identity.
- No installer, no LaunchAgent, no rollback tooling yet.
- Loopback HTTP only; cookies are not marked Secure because the transport is same-Mac.
- Simulator thermal model is arbitrary (2.5 °F/min heating, 1 °F/min cooling).
