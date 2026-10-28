# Recovery Room sauna controller

Independently owned controller for one Clearlight sauna on an always-running shop Mac.
**Current build is read-only**: it observes the sauna and shows a staff dashboard. It cannot
send any control command. See `docs/plan-and-inputs.md` for the plan and the inputs still needed,
and `docs/unverified.md` for everything not yet proven against the real unit.

Requirements: Python 3.11+ (macOS: `brew install python` or python.org). No runtime dependencies.

## Run the simulated dashboard (no hardware)

```bash
python3 -m recoveryroom_sauna serve --config config/simulated.config.json
```

In a second terminal, open pairing and enter the PIN at http://127.0.0.1:8787/:

```bash
python3 -m recoveryroom_sauna pair --config config/simulated.config.json
```

## Read-only field test on the shop Mac (gate A)

First find the sauna's address. This only attempts TCP connections on the sauna port across the Mac's own subnet:

```bash
python3 tools/find_sauna.py
```

Then run the diagnostic with the address it reports, while watching the physical panel. It never sends a control frame.

```bash
python3 tools/lan_diagnose.py --host <SAUNA_PRIVATE_IP> --seconds 120 --out lan_diagnose_report.json
```

Send back `lan_diagnose_report.json` and note whether power, lights, target and timer matched the panel.

## Run against the real sauna (status only)

Copy `config/example.config.json` to a private location, set `saunaHost`, keep everything else
as is, then:

```bash
python3 -m recoveryroom_sauna check-config --config /path/to/config.json
python3 -m recoveryroom_sauna serve --config /path/to/config.json
```

Other commands: `pair`, `sessions`, `revoke --id <id>`, `simulate`.

## Tests

```bash
python3 -m pip install pytest
python3 -m pytest -q
```

Unit and HTTP tests never connect to a real appliance.

## Layout

```
recoveryroom_sauna/   protocol, simulator, device (LAN worker), business_hours, config, security, server, static UI
tests/                simulated tests only
tools/                find_sauna.py (LAN port scan), lan_diagnose.py (read-only field diagnostic)
config/               non-secret example configs
docs/                 plan-and-inputs.md, unverified.md
private/              (never created in Git) sessions, pairing, future state and credentials
```
