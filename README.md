# Netcode Platform

Network-as-code training platform for guided changes, transparent artifacts,
validation, Arista lab testing, and evidence reports.

The first vertical slice is the `add_vlan` workflow:

```text
wizard -> YAML intent -> Jinja template -> EOS config -> validation -> Git diff -> adapter contract -> durable job -> lab dry-run -> lab apply -> report
```

The current platform core includes:

- Guided UI and CLI workflow for an Arista EOS VLAN change.
- YAML/Jinja/static validation artifact chain, visible to the engineer.
- Durable SQLite change and job records under `.netcode/netcode.db`.
- Execution adapter registry for Arista EOS config-session dry-run, apply,
  rollback, and verification.
- Rez driver bridge for state adapters. The bridge imports Rez from
  `NETCODE_REZ_ROOT`, `/home/syedhussain/resonance-core`, or the local
  `Claude/resonance-core` checkout when available, and degrades cleanly when
  external Rez dependencies are not installed.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
netcode init
netcode pipeline intents/examples/add_guest_vlan.yaml
netcode adapters list
netcode ui --reload
```

Open the UI at:

```text
http://127.0.0.1:8088
```

The UI is also served at `/app`. If another local service already owns port
`8088`, run Netcode on a different port:

```bash
netcode ui --port 8089
```

Then open:

```text
http://127.0.0.1:8089/app
```

## ORB / Containerlab Testing

The Arista containerlab network is reachable from the `clab` ORB VM, not from
the macOS host.

If this workspace is not accessible inside ORB because it lives under
`~/Documents`, copy it into an ORB-accessible path first:

```bash
tar --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude '*.egg-info' -czf /tmp/netcode-platform.tgz .
orb -m clab bash -lc "rm -rf /tmp/netcode-platform-test && mkdir -p /tmp/netcode-platform-test && cat > /tmp/netcode-platform.tgz && tar -xzf /tmp/netcode-platform.tgz -C /tmp/netcode-platform-test" < /tmp/netcode-platform.tgz
orb -m clab bash -lc "cd /tmp/netcode-platform-test && python3 -m pip install -e '.[dev]' && python3 -m netcode.cli pipeline intents/examples/add_guest_vlan.yaml"
orb -m clab bash -lc "cd /tmp/netcode-platform-test && python3 -m netcode.cli lab dry-run intents/examples/add_guest_vlan.yaml --device v2-store1"
```

Confirm Rez adapter discovery inside ORB:

```bash
orb -m clab bash -lc "cd /tmp/netcode-platform-test && python3 -m netcode.cli adapters list"
orb -m clab bash -lc "cd /tmp/netcode-platform-test && python3 -m netcode.cli adapters device v2-store1"
```

Run the complete Arista lab path:

```bash
orb -m clab bash -lc "cd /tmp/netcode-platform-test && python3 -m netcode.cli lab full-run intents/examples/add_guest_vlan.yaml --device v2-store1 --apply"
```

Rollback the lab VLAN:

```bash
orb -m clab bash -lc "cd /tmp/netcode-platform-test && python3 -m netcode.cli lab rollback intents/examples/add_guest_vlan.yaml --device v2-store1"
```

Inspect persisted platform records:

```bash
orb -m clab bash -lc "cd /tmp/netcode-platform-test && python3 -m netcode.cli changes list"
orb -m clab bash -lc "cd /tmp/netcode-platform-test && python3 -m netcode.cli changes jobs"
```

Run the lab-capable UI inside ORB:

```bash
orb -m clab bash -lc "cd /tmp/netcode-platform-test && python3 -m netcode.cli ui --host 0.0.0.0 --port 8090"
```

If macOS can route directly to the ORB machine, open:

```text
http://192.168.139.83:8090
```

If direct routing is blocked, create a localhost tunnel from macOS:

```bash
ssh -N -L 8091:127.0.0.1:8090 clab@orb
```

Then open:

```text
http://127.0.0.1:8091
```

The default lab inventory points at the Rez Arista lab v2 addresses:

- `v2-store1`: `172.100.1.41`
- `v2-store2`: `172.100.1.42`
- `v2-store3`: `172.100.1.43`

Credentials default to `admin/admin` for the lab.
