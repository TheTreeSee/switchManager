# switchManager

Configure Cisco Layer 2 / Layer 3 switches from `;`-delimited CSV files: generate the IOS configuration,
apply it over SSH with [Netmiko](https://github.com/ktbyers/netmiko) and back up the running config through a
built-in TFTP server.

Howest MCT – Infrastructure Automation, Opdracht 2. Design: [PROJECT_PLAN.md](PROJECT_PLAN.md),
assignment and the teacher's example files: [docs/assignment/](docs/assignment).

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # Netmiko (only needed for --detect, --apply, --backup)
cp .env.example .env                     # fill in the SSH user; the password may stay empty (asked once)
```

## Workflow

```bash
# 1. list the switches (only the IP is required)
printf 'ip;hostname;note\n172.19.67.13;;\n172.19.67.10;;\n' > inventory/switches.csv

# 2. discover hostname, model and the real interface names (read-only)
python switch_manager.py --detect                  # -> inventory/discovery.json, inventory/interface-report.txt

# 3. one CSV per switch, named after its IP: input/172.19.67.13.csv
python switch_manager.py --gen                     # every input/*.csv -> output/<ip>.cfg (default mode)
python switch_manager.py --gen 172.19.67.13.csv

# 4. apply
python switch_manager.py 172.19.67.13 --apply                         # CSV defaults to input/<ip>.csv
python switch_manager.py 172.19.67.13 --apply other.csv --rollback-timer 10
python switch_manager.py --batch --apply                              # every input/<ip>.csv to its switch

# backup only
python switch_manager.py 172.19.67.13 --backup     # -> backup/<ip>_<hostname>_<time>.cfg
```

Options: `-y/--yes` (no confirmation), `--set-hostname` (also configure the inventory hostname),
`--no-backup` (skip the backup after an apply), `--examples` (use the example folders below, works offline).

## CSV format

```csv
Vlan;Description;IP Address;Netmask;Switch;Ports
1982;CD-WIFI;172.19.82.1;255.255.255.0;1;1-28
1982;CD-WIFI;;;2;1-28
1967;CD-Management;172.19.67.13;255.255.255.0;;
1967,1969,1982;Uplink to core;;;1;25-26
```

| Row | Meaning |
|---|---|
| IP address + netmask filled in | Layer 3 VLAN: `interface Vlan<id>` with that address |
| no IP details | Layer 2 VLAN |
| description contains `management` or `mgt` | management VLAN (usually without ports); a Layer 2 switch gets `ip default-gateway` from its network |
| description contains `trunk` or `uplink` | trunk ports; a filled-in `Vlan` field is the allowed-VLAN list (VLAN filtering), empty = all |
| every (non-management) VLAN has an IP | `ip routing` (Layer 3 switch) |

`Switch` is the stack member (empty = 1). `Ports` and trunk VLANs accept lists and ranges: `1,5,9,220,1580-1590,2034`.
The same VLAN may appear on several rows (e.g. ports on two stack members). The header may also be written as
`vlan; description; ip address; subnetmask; switch; ports;`.

Validation stops with every problem listed (with line numbers): a port used twice, a reversed range, a VLAN
list on a non-trunk row, two management VLANs, an IP without mask, conflicting SVI addresses, …

## Port numbers → real interfaces

Interface names depend on the model, so nothing is assumed: `--detect` reads `show ip interface brief` and per
stack member the physical ports form one sequence (module, then speed, then number). CSV port *N* is the *N*-th
port of that sequence — see `inventory/interface-report.txt`:

```text
172.19.67.13 - BST-D-1-242 (WS-C2960-24TT-L)
    ports 1-24     FastEthernet0/1-24
    ports 25-26    GigabitEthernet0/1-2
172.19.67.10 - BST-C-Core-2 (WS-C3850-48P)
  switch 2
    ports 1-48     GigabitEthernet2/0/1-48
    ports 49-52    GigabitEthernet2/1/1-4
```

A port or stack member that does not exist fails validation. `--gen` uses the saved discovery;
`--apply` always reads the interfaces live right before configuring.

## Example scenarios

`input/examples/` (never read by a normal run) with matching discovery data in `inventory/examples/` and the
generated configs in [`output/examples/`](output/examples):

| File | Scenario |
|---|---|
| `172.19.67.10.csv` | Layer 3, 2-member 3850 stack (teacher's `BST-C-Core-2.csv`) |
| `172.19.67.13.csv` | Layer 2 access switch, default gateway from the management VLAN (teacher's `BST-D-1-242.csv`) |
| `172.19.67.14.csv` | Layer 2 with an uplink trunk that only allows 4 VLANs |
| `10.10.10.1.csv` | Layer 3 3560 with a trunk for all VLANs and a filtered uplink (`encapsulation dot1q` added for this ISL-capable model) |
| `192.168.99.20.csv` | management VLAN only |

```bash
python switch_manager.py --examples --gen
```

## Apply: staging, rollback and results

Per switch: validate → write `output/<ip>.cfg` → preview + confirmation → send in stages:

1. **base** – VLANs (and the hostname with `--set-hostname`)
2. **access** – access ports
3. **management** – SVIs, management VLAN, `ip default-gateway`, `ip routing`, trunks/uplinks — last, because
   these can cut the SSH session

Then the tool checks the session (reconnects up to 3× if the management change dropped it), runs `write memory`,
checks the startup config and, with `BACKUP_AFTER_APPLY=true`, downloads the new config through TFTP.
A command the switch rejects (`% Invalid input …`) stops the run before the management stage.

**`--rollback-timer N`**: after checking that no reload is pending (an existing one is never replaced), the tool
schedules `reload in N`, and sends `reload cancel` only after a verified save. If the reload happens, the switch boots
its startup config — unsaved running changes, also older ones, are lost.

| Result | Meaning |
|---|---|
| `SUCCESS` | applied, saved and verified |
| `FAILED` | validation, connection or configuration error — see the log |
| `SKIPPED` | not confirmed, or no input file in a batch |
| `CONNECTION_LOST_ROLLBACK_PENDING` | session lost before saving; the scheduled reload should restore the saved config |
| `CONNECTION_LOST_STATE_UNKNOWN` | session lost without rollback timer — check the switch out-of-band |
| `SAVE_RESULT_UNKNOWN` | session lost after `write memory` was sent — the change may or may not be saved |
| `RELOAD_CANCEL_UNKNOWN` | saved, but `reload cancel` not confirmed — the switch may still reload |

Every apply writes its own log, `logs/<UTC time>_<ip>_apply.log`: inventory hostname, input and output file with
SHA-256, the live interface list, every command block sent with the raw switch output, save/verify/rollback steps and
any traceback. The tool never retries on its own.

### Batch

`--batch --apply` matches `input/<ip>.csv` to the inventory line with exactly that IP — never on a hostname. Invalid
file names or files without an inventory line refuse the whole batch. The plan (IP ← file, L2/L3, number of
commands) is shown once and confirmed once. Switches run in inventory order, so **put downstream switches before
their uplink switch** in `inventory/switches.csv`; `BATCH_MAX_WORKERS` > 1 opts into parallel runs. A failure only
affects that switch; the summary lists successes, failures, uncertain states and skipped switches with their logs,
and the exit code is non-zero when anything failed.

## Backup (TFTP)

`--backup` starts a temporary TFTP server (`TFTP_BIND_ADDRESS`:`TFTP_PORT`), runs
`copy running-config tftp://<this PC>/<file>` on the switch and stores the file in `backup/`. The address the switch
must reach is `TFTP_SERVER_ADDRESS`, or automatically the local address on the route to the switch. The server only
accepts the one expected file name. Port 69 needs extra rights on Linux:

```bash
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=69
```

Unreachable TFTP (switch error, no route, firewall) is reported with the switch's own message.

## Settings (`.env`)

See [`.env.example`](.env.example): SSH user/password/enable secret, `BATCH_MAX_WORKERS`, `MGMT_GATEWAY`
(`first` | `last` | an address), `TRUNK_ENCAPSULATION` (`auto` | `yes` | `no`), TFTP and backup settings.
Real environment variables override `.env`. `.env`, `input/`, `output/`, `inventory/`, `logs/` and `backup/` are
git-ignored except their `examples/` folders.

## Decisions on the open items in PROJECT_PLAN.md

1. **Default gateway** of a Layer 2 switch: the first usable address of the management network; `MGMT_GATEWAY=last`
   or an explicit address overrides it. A gateway equal to the switch's own address is refused.
2. **Layer 3**: `ip routing`; on ISL-capable models (3550/3560/3750/4500/…) trunks also get
   `switchport trunk encapsulation dot1q` (`TRUNK_ENCAPSULATION`).
3. **Hostname**: opt-in with `--set-hostname`; discovery never changes the switch.
4. **Scenarios**: the five example CSVs above plus invalid inputs in the tests.

## Tests

```bash
python -m unittest discover -s tests -t .
```

57 tests, no switch needed: CSV parsing and validation, interface resolution, rendering of every example (the
committed example outputs must match), and apply against a fake IOS switch — staging order, rejected commands,
rollback timer, every connection-lost state, reconnect after a management change, batch matching and partial
failures, and a real TFTP transfer.

## Structure

```text
switch_manager.py          entry point
switch_manager/
  cli.py                   command line
  config.py                .env / environment settings
  models.py                validated data models
  csv_parser.py            CSV, ranges, validation, L2/L3 and gateway rules
  inventory.py             inventory/switches.csv and discovery.json
  discovery.py             --detect (read-only)
  interface_map.py         CSV ports -> real interfaces
  renderer.py              IOS configuration in stages
  netmiko_client.py        SSH connections
  apply.py                 staged apply, rollback timer, result states
  batch.py                 --batch --apply
  apply_log.py             per-switch logs
  backup.py                TFTP server + copy running-config tftp:
tests/                     unit tests + fake IOS switch
```
