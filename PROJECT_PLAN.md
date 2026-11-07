# Switch Manager — Project Review

## Purpose

`switchManager` is a Python command-line tool for deploying Cisco Layer 2 and
Layer 3 switch configurations from semicolon-delimited CSV files.  It supports
single-switch and fleet workflows, discovers model-specific interfaces before
configuration, applies configuration over SSH with Netmiko, and backs up
running configurations through a local TFTP server.

The project is intended for a public GitHub repository.  Credentials, customer
addresses, generated configurations, discoveries, and backups must therefore
remain local and untracked.

## Technology and boundaries

- Python
- Netmiko for Cisco SSH connections and sending configuration commands
- A local TFTP server only for the backup workflow
- Cisco IOS is the first supported platform (`cisco_ios`)
- Connection credentials and defaults are read from `.env`; no important value
  is hardcoded
- `.env.example` documents all required environment variables using safe
  placeholders

## Runtime directories

```text
switchManager/
├── input/
│   ├── examples/             # tracked sample input; never read by the tool
│   └── <real switch CSVs>    # ignored
├── output/
│   ├── examples/             # tracked generated-config examples
│   └── <generated configs>   # ignored
├── inventory/
│   ├── examples/             # tracked inventory/discovery examples
│   ├── switches.csv          # ignored
│   ├── discovery.json        # ignored
│   └── interface-report.txt  # ignored
├── logs/                     # ignored per-switch apply logs
└── backup/                   # ignored running-config backups
```

The tool only scans CSV files directly inside `input/`; it must never process
files in `input/examples/`.

The eventual `.gitignore` rules will ignore each runtime directory's content
while explicitly allowing its `examples/` subtree to be committed.  `logs/`,
`backup/`, and `.env` are also ignored.

## Inventory

The operator should enter the smallest possible amount of information.  The
editable inventory file is `inventory/switches.csv`:

```csv
ip;hostname;note
172.19.67.10;;
172.19.67.13;;
```

Only `ip` is required.  `hostname` is filled from discovery and may be edited
by the operator.  `note` is optional and is for human context only.

Example after discovery:

```csv
ip;hostname;note
172.19.67.10;BST-C-Core-2;2-member stack
172.19.67.13;BST-D-1-242;access switch
```

No credentials, device type, interface format, or enable/disable flag belongs
in this file.

## Device discovery

### Command

```bash
python switch_manager.py --detect
```

`--detect` connects to every address in `inventory/switches.csv` and collects:

- current configured hostname
- Cisco platform/software information
- physical interfaces from `show ip interface brief`
- stack information where available, such as from `show switch`
- reachability, authentication, and parsing errors

The command updates the hostname column when a hostname is found and writes
technical discovery information to `inventory/discovery.json`.  It also writes
a human-readable `inventory/interface-report.txt`, listing the physical port
ranges available on every device, for example:

```text
172.19.67.13 — Switch
  FastEthernet0/1-24
  GigabitEthernet0/1-2

172.19.67.10 — BST-C-Core-2
  GigabitEthernet1/0/1-48
  GigabitEthernet2/0/1-48
```

Discovery is safe: it must not change a switch hostname or any other device
configuration.  Setting a configured hostname is an explicit apply action,
not a discovery side effect.

## CSV input format

Every file represents one target switch or stack and uses semicolon delimiters:

```csv
Vlan;Description;IP Address;Netmask;Switch;Ports
```

Meaning:

- `Vlan`: VLAN ID, or an allowed-VLAN expression on trunk/uplink rows
- `Description`: VLAN or interface description
- `IP Address` and `Netmask`: SVI address details; blank for Layer 2 VLANs
- `Switch`: stack member when the device is a stack; may be `1` for a
  non-stacked device
- `Ports`: access or trunk ports, accepting comma-separated values and ranges

Port and VLAN expressions accept individual values and inclusive ranges, such
as `1,5,9,220,1580-1590,2034`.

### Deterministic batch target matching

Batch apply must never use a hostname, description, note, or other
human-editable label to choose a target device.  The input CSV filename is the
target's IPv4 address exactly, followed by `.csv`:

```text
input/172.19.67.10.csv
input/172.19.67.13.csv
```

`--batch --apply` parses the address from the filename and requires one exact
matching `ip` record in `inventory/switches.csv`.  It rejects invalid filenames,
duplicate inventory addresses, duplicate target CSVs, and input files with no
inventory match.  A CSV is therefore tied deterministically to the switch that
will receive it, even if hostnames change, contain spaces, are duplicated, or
are mistyped.

### Interpretation rules

- A row with an IP address and netmask defines a Layer 3 VLAN SVI.
- A row without IP details defines a Layer 2 VLAN.
- A management VLAN is identified by a description containing `management` or
  `mgt`.  Its SVI usually has no physical ports.
- The default gateway for a Layer 2 switch is derived from the management VLAN
  network.
- A description containing `trunk` or `uplink` identifies a trunk interface.
  When its `Vlan` field is populated, it specifies the allowed VLAN expression.
- When all defined VLANs contain IP details, Layer 3 routing is enabled.

The description-based management/trunk identification is required for initial
compatibility.  A future format may add an explicit type/role column to remove
that inference.

## Interface resolution and validation

Cisco physical interface naming is model dependent.  The tool must not assume
a fixed format such as `Gi1/0/1` or `Fa0/1`.

Before generating configuration, it resolves each CSV `Switch`/`Ports` request
against the physical interfaces discovered from the actual target device.  It
supports forms such as:

- `FastEthernet0/1` (`Fa0/1`)
- `FastEthernet0/0/1` (`Fa0/0/1`)
- `GigabitEthernet0/1` (`Gi0/1`)
- `GigabitEthernet1/0/1` (`Gi1/0/1`)
- `TenGigabitEthernet1/1/1`

For a stack, the CSV member value maps to a discovered stack-member interface
prefix.  For a non-stacked switch, the resolver maps its listed ports to the
available physical port sequence.  It only targets discovered physical
interfaces and must fail validation rather than generate a configuration for a
nonexistent port.

`--apply` always refreshes or verifies the live interface map immediately
before sending configuration.  `--gen` uses saved discovery data and warns or
fails clearly if none is available.

## Commands and behaviour

```bash
# Generate configuration (default mode)
python switch_manager.py --gen <csv-name>

# Discover all inventory devices
python switch_manager.py --detect

# Generate and apply to one switch
python switch_manager.py <switch-ip> --apply <csv-name>

# Batch apply every input CSV to its inventory target
python switch_manager.py --batch --apply

# Apply with a 10-minute reload safety net for management-path changes
python switch_manager.py <switch-ip> --apply <csv-name> --rollback-timer 10

# Backup a single switch only
python switch_manager.py <switch-ip> --backup
```

### Safety expectations

- `--apply` writes the generated configuration to `output/` before sending it.
- It previews the target, input file, and validation result, then requires
  confirmation unless an explicit non-interactive option is supplied.
- A successful apply saves the running configuration.
- `--batch --apply` shows the complete IP-to-input-file plan before work begins
  and requires one explicit batch confirmation (or a non-interactive option).
- Backup mode overrides the normal generation mode when it is the only
  requested action.

## Apply logging, partial failures, and batch concurrency

Every apply creates a dedicated, timestamped, per-switch log file under
`logs/`, for example:

```text
logs/2026-09-28T14-03-11Z_172.19.67.13_apply.log
```

Each log records the target IP, inventory hostname at the time of the run,
input CSV path and checksum, generated output path and checksum, start/end
timestamps, validation result, connection events, commands sent, raw command
output returned by the switch, save-config result, and any exception/traceback.
Logs are append-only for their individual execution and are never shared by
two switches.

`--batch --apply` can run different switches in parallel.  The maximum number
of concurrent workers is set through an environment variable documented in
`.env.example` (for example, `BATCH_MAX_WORKERS`).  Its default is **1**:
batch apply is serial and in inventory-file order unless an operator explicitly
opts into a higher value.  Every switch gets its own Netmiko connection,
generated-output file, and log file.  Console output is presented as completed
per-switch results rather than interleaved device command output.

Parallel operation is an opt-in tradeoff, not a normal deployment setting.  A
trunk or other management-path change on an upstream switch can interrupt SSH
to switches behind it.  Before a serial batch run, the operator must place
downstream/access switches before their upstream/distribution switches in
`inventory/switches.csv`; the tool preserves that order.  It cannot infer
network topology from IP addresses or safely invent that ordering.

### Management-path staging and rollback timer

An active SSH session can be lost when configuring the management SVI, the
Layer 2 `ip default-gateway`, routed interfaces, `ip routing`, or an uplink
trunk/allowed-VLAN list.  Apply is therefore staged for each switch:

1. Validate the full configuration and write the candidate file to `output/`.
2. Send VLAN creation and non-management access-port changes.
3. Send management-path changes last: management SVI/IP, default gateway,
   routing enablement, and trunk/uplink configuration.
4. Confirm the session remains usable, save the configuration, then verify the
   saved state.

For operations that contain management-path changes, the tool offers a timed
reload safety net: schedule `reload in <minutes>` before changing the device;
after reconnecting, confirming the configuration, and saving it, send `reload
cancel`.  The duration is configurable and must be long enough for the
operator to recover connectivity.

Timed reload is only safe to schedule after a preflight check confirms that no
reload is already pending and the operator explicitly enables rollback
protection for the run.  It must not silently replace an existing reload
schedule.  The operator is warned that a reload restores the startup
configuration and may discard any unrelated, pre-existing unsaved running
changes.  The log records scheduling, confirmation, cancellation, and all
corresponding raw device output.

Failure handling is isolated per switch:

- A parse, validation, connection, configuration, save, or logging problem
  produces a non-success result only for that IP.
- A failure never aborts work already running or scheduled for other switches.
- The batch waits for all submitted switch tasks, then prints a final summary
  of successes, failures, skipped targets, and their log paths.
- The process exits non-zero if any target failed, so automation can detect a
  partial fleet deployment.
- The tool does not automatically retry an apply; the log must be reviewed and
  a new explicit apply run started.

A lost SSH session during or after management-path work is never reported as a
plain `apply failed`, because some, all, or none of the commands may have been
accepted.  It receives one of these explicit terminal statuses:

- `CONNECTION_LOST_ROLLBACK_PENDING`: rollback protection was scheduled but
  the session was lost **before the save command was sent**.  The candidate may
  be running temporarily; it should revert at the recorded reload time unless
  an operator intervenes.
- `CONNECTION_LOST_STATE_UNKNOWN`: no rollback timer was active and the tool
  could not determine whether the switch accepted the last commands or saved
  them.  Manual out-of-band/reconnect verification is required.
- `SAVE_RESULT_UNKNOWN`: the save command was sent but its result could not be
  read because the session dropped.  The startup configuration may or may not
  contain the change; this requires verification.  A scheduled reload is *not*
  a guaranteed rollback in this state, because the save may already have
  succeeded before the disconnect.
- `RELOAD_CANCEL_UNKNOWN`: saving was confirmed, but the result of `reload
  cancel` could not be confirmed.  The switch may still reload at the recorded
  time, booting whatever configuration was saved; the operator must reconnect
  and cancel/verify it before then.

These states are counted separately from ordinary validation and connection
failures in the batch summary, include the exact last confirmed command/output
in the isolated log, and always produce a non-zero process exit code.

## Backup workflow

`--backup` starts a temporary local TFTP server, requests the target switch to
copy its running configuration to the configured TFTP host, and stores the
result in `backup/`.  The local TFTP bind address, advertised server address,
and destination directory are configurable through environment variables.

The tool must clearly report TFTP reachability failures; the switch must be
able to route to the host running the tool.

## Proposed implementation structure

```text
switch_manager/
├── cli.py             # command-line parsing and orchestration
├── models.py          # validated data models
├── csv_parser.py      # semicolon CSV and range parsing
├── discovery.py       # SSH discovery and interface-map persistence
├── interface_map.py   # CSV port-to-interface resolution
├── renderer.py        # Cisco IOS configuration rendering
├── netmiko_client.py  # connection and apply operations
├── apply_log.py       # isolated raw-output logging for each apply
└── backup.py          # TFTP server and backup operations

tests/
├── test_csv_parser.py
├── test_interface_map.py
├── test_renderer.py
└── fixtures/
```

## Delivery order

1. Create the project skeleton, safe `.gitignore`, example directories, and
   `.env.example`.
2. Implement CSV parsing, range expansion, validation, and unit tests.
3. Implement `--detect`, discovery persistence, and interface reporting.
4. Implement configuration rendering with test fixtures and example outputs.
5. Implement safe single-switch application through Netmiko and isolated
   per-switch logs.
6. Implement IP filename-to-inventory matching, parallel batch application,
   and partial-failure summaries.
7. Implement and test the TFTP backup workflow.

## Open items for the next review

1. Confirm the exact default-gateway rule for management VLANs: conventionally
   the first usable address in its subnet, unless a different gateway is
   supplied.
2. Confirm the Cisco commands and expected behaviour for enabling Layer 3
   routing (`ip routing`) on the intended switch families.
3. Decide whether hostname configuration is part of normal `--apply` or a
   separate opt-in flag.
4. Define a representative set of test scenarios: standalone Layer 2,
   standalone Layer 3, stack, management-only, trunk filtering, and invalid
   ports/ranges.
