# obicfg command reference

Every command, every flag, and the exact shape of every `--json` output.

- [Global options](#global-options)
- [Exit codes](#exit-codes)
- [Parameter paths](#parameter-paths)
- Reading: [`pages`](#pages) · [`show`](#show) · [`search`](#search) · [`get`](#get) · [`status`](#status) · [`telemetry`](#telemetry)
- Writing: [`set`](#set) · [`unset`](#unset) · [`apply`](#apply)
- Backups: [`dump`](#dump) · [`diff`](#diff)
- Device: [`reboot`](#reboot) · [`probe`](#probe)
- [Profile format](#profile-format)
- [Config file format](#config-file-format)

---

## Global options

These may appear **before or after** the subcommand: `obicfg --host X status` and
`obicfg status --host X` are identical.

| Flag | Env | Default | Meaning |
|---|---|---|---|
| `-H`, `--host` | `OBI_HOST` | — | Device address. Required, from flag, env or config |
| `-u`, `--user` | `OBI_USERNAME` | `admin` | Admin username |
| `-p`, `--password` | `OBI_PASSWORD` | `admin` | Admin password. **Prefer the alternatives** — argv is visible in `ps` |
| `--password-file` | `OBI_PASSWORD_FILE` | — | Read the password from a file (trailing newline stripped) |
| `--port` | `OBI_PORT` | scheme default | Admin port, if moved |
| `--scheme` | `OBI_SCHEME` | `http` | `http` or `https` |
| `--timeout` | `OBI_TIMEOUT` | `15.0` | Per-request timeout, seconds |
| `--transport` | `OBI_TRANSPORT` | `paramlist` | Write encoding — see [How writes work](../README.md#how-writes-actually-work) |
| `--unsafe` | — | off | Disable write protection. See [Write protection](../README.md#write-protection) |
| `-y`, `--yes` | — | off | Skip **every** confirmation prompt, for every command in that invocation |

Precedence: flag → environment → config file → built-in default.

`obicfg --version` prints the version. Unlike the flags above it must come
*before* the subcommand — `obicfg status --version` is an error.

> `-y` is not scoped to one prompt. A single `-y` disarms the confirmation on
> `unset`, `apply`, `reboot` and `probe` alike. Pass it only for an action a
> human has already approved.

On a non-interactive run (a script, cron, CI) those same four commands refuse
to assume an answer and exit 1 rather than defaulting to "no".

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Error — unreachable, bad credentials, no such page or parameter, invalid value. Also `diff` when it finds differences, and `search` when nothing matches |
| `2` | Bad usage — malformed arguments |
| `3` | The device accepted a write and did not apply it |
| `4` | Blocked by write protection |

Exit `0` from a write does **not** by itself mean a setting changed. Check
that `applied[]` is non-empty and every entry in it has `"verified": true`,
or read the `ok`/`FAIL` lines in the text output.

Exit 1 covers several distinct situations — unreachable device, bad
credentials, unknown page or parameter, a value the device's syntax rejects,
an unreadable snapshot, and `diff` finding differences. To tell them apart,
pass `--json`: **every error is also emitted as JSON on stdout** when you do,
with a `kind` field (`unreachable`, `auth`, `not_found`, `invalid_value`,
`usage`, `guard`, `not_applied`, `error`) alongside `error` and `exit`. The
human-readable message always goes to stderr as well.

```console
$ obicfg get sp2.NoSuchThing --json
{"error": "page VS_1_VP_1_L_2_ (SP2 Service) has no parameter 'NoSuchThing'.",
 "kind": "not_found", "exit": 1}
```

## Parameter paths

Every parameter is addressed as `<page>.<parameter>`:

```
sp2.X_InboundCallRoute            # alias form
VS_1_VP_1_L_2_.X_InboundCallRoute # raw form -- always works
itsp.b.sip.ProxyServer            # aliases may contain dots
```

Page aliases and the SP/ITSP numbering trap are documented in
[`skills/obicfg/references/recipes.md`](../skills/obicfg/references/recipes.md).
`obicfg pages` lists what a given device has; `obicfg search` finds a parameter
without knowing either name.

---

## `pages`

List the configuration pages this device exposes, read from its own menu.

```sh
obicfg pages [--json] [--titles]
```

| Flag | Meaning |
|---|---|
| `--titles` | Also show each page's own title. Costs one fetch per page; menu labels repeat ("General", "SIP") because the menu is a tree, while titles are unique |

```console
$ obicfg pages
PAGE                 MENU LABEL         ALIASES
-------------------  -----------------  -----------------
DI_S_                System Status      status, system
VS_1_VP_1_L_2_       SP2                sp2
```

```json
[{"page": "VS_1_VP_1_L_2_", "label": "SP2", "aliases": ["sp2"]}]
```

The menu is not a complete index of the device. `DM_S_` (auto-provisioning)
and `callhistory.xml` are served on request but appear in no menu entry, so
they will not be listed here — they are still reachable by name.

## `show`

List the parameters on one page, with their values, types and hashes.

```sh
obicfg show <PAGE> [--json] [--changed]
```

| Flag | Meaning |
|---|---|
| `--changed` | Only parameters the device records as explicitly set (see the note below — this is not quite "differs from default") |

```console
$ obicfg show sp2 --changed
VS_1_VP_1_L_2_  —  SP2 Service

PARAMETER           VALUE   STATE  HASH
------------------  ------  -----  --------
Enable              true    set    2ff30fcb
X_InboundCallRoute  sp1     set    80fd3f53
```

`STATE` is `default` or `set`; a trailing `ro` marks a read-only readout.

```json
{
  "page": "VS_1_VP_1_L_2_",
  "title": "SP2 Service",
  "reboot_required": false,
  "parameters": [
    {
      "name": "ProxyServerTransport",
      "path": "VS_1_VP_1_L_2_.ProxyServerTransport",
      "hash": "c181548e",
      "value": "UDP",
      "default": "UDP",
      "is_default": true,
      "writable": true,
      "type": "string",
      "options": ["UDP", "TCP", "TLS"],
      "max_length": null,
      "min": null,
      "max": null,
      "description": "Transport protocol to connect to SIP server"
    }
  ]
}
```

`options` is the device's own enumeration — the authoritative list of legal
values for that parameter. `type`, `max_length`, `min` and `max` are the rest
of its declared syntax, and are `null` where the device declares no limit.
These four are what to check a value against after an exit 3.

`--changed` filters on the device's own "has been written" flag, not on
whether the value differs from the default. A parameter that was explicitly
set to the same string as its default still shows up. That is the device's
distinction, not this tool's.

## `search`

Find parameters by name, path or description, across every configuration page.

```sh
obicfg search <REGEX> [--json]
```

The pattern is a **regular expression**, matched case-insensitively against
each parameter's path and its description. Search for a keyword — `search
inbound` — not a sentence; a plain-English phrase matches nothing. **Exits 1 when nothing matches**,
in both output forms.

```console
$ obicfg search 'InboundCallRoute'
PATH                               VALUE  DESCRIPTION
---------------------------------  -----  ------------------------------------
VS_1_VP_1_L_1_.X_InboundCallRoute  sp2    Routing rule for inbound calls
```

```json
[{"path": "...", "hash": "80fd3f53", "value": "sp2", "is_default": false,
  "description": "Routing rule for inbound calls on this trunk"}]
```

## `get`

Print the current effective value of one or more parameters.

```sh
obicfg get <PATH> [<PATH> ...] [--json]
```

One path prints the bare value (script-friendly); several print
`path = value` lines. JSON is always an object keyed by the **canonical raw
path**, not by the alias you asked with: request `itsp.b.sip.ProxyServer` and
the key comes back as `VS_1_VP_2_SIP_.ProxyServer`.

```console
$ obicfg get sp2.URI
200
$ obicfg get sp2.URI sp3.URI
VS_1_VP_1_L_2_.URI = 200
VS_1_VP_1_L_3_.URI = 201
```

"Effective" means the device's `current` value if it has one, otherwise the
factory default — the two are distinct in the XML and conflating them is the
easiest way to misread a device.

## `set`

Write one or more parameters, then read them back to confirm.

```sh
obicfg set <PATH>=<VALUE> [<PATH>=<VALUE> ...] [--json] [-n|--dry-run]
          [--no-verify] [--reboot]
```

| Flag | Meaning |
|---|---|
| `-n`, `--dry-run` | Show the plan; send nothing |
| `--no-verify` | Skip the read-back. **Not advised** — the device returns HTTP 200 for writes it discards, so without the read-back success is unknowable |
| `--reboot` | Reboot afterwards and wait. Skipped if any write failed verification |

Values are validated against the device's declared syntax — enumerations,
integer ranges, string lengths — before anything is sent. Settings on the same
page go out in **one request**, matching what the web UI does on Submit.

`<VALUE>` may be `@filename` to read the value from a file, which is how to
pass a long `DigitMap` or `OutboundCallRoute` without the shell mangling it.
Leading and trailing whitespace in the file is stripped, so a trailing
newline does not become part of the value.
The literal string `default` is rejected: the device reserves it as the
reset-to-factory sentinel — use [`unset`](#unset).

```console
$ obicfg set sp2.X_InboundCallRoute=sp1 sp2.X_UserAgentPort=5061
plan:
  ~ VS_1_VP_1_L_2_.X_InboundCallRoute: 'ph' -> 'sp1'
  ~ VS_1_VP_1_L_2_.X_UserAgentPort: '5060' -> '5061'

  ok   VS_1_VP_1_L_2_.X_InboundCallRoute = 'sp1'
  ok   VS_1_VP_1_L_2_.X_UserAgentPort = '5061'
```

```json
{
  "dry_run": false,
  "verified": true,
  "plan":    [{"path": "...", "old": "ph", "new": "sp1", "noop": false}],
  "applied": [{"path": "...", "value": "sp1", "verified": true, "observed": "sp1"}],
  "failures": []
}
```

| Field | Meaning |
|---|---|
| `dry_run` | `--dry-run` was passed. Nothing was sent — but note the converse does not hold: a run where every change was already correct also sends nothing, with `dry_run: false`. Read `applied[]` to know whether anything reached the device |
| `verified` | A read-back was performed. `false` under `--no-verify`, and `false` on a dry run — a preview verifies nothing |
| `plan[].noop` | Already at the requested value; it will not be sent |
| `applied[]` | Requests that went out — **not** proof they took effect |
| `applied[].verified` | The device confirms the new value. This is the success signal |
| `failures[]` | Accepted but not applied. Non-empty means exit 3 |

## `unset`

Restore parameters to their factory defaults.

```sh
obicfg unset <PATH> [<PATH> ...] [--json] [-n|--dry-run]
```

A reset is destructive in the same way a write is, so it takes the same dry
run and prompts before acting (`-y` skips the prompt). The mechanism is the
device's own: the literal string `default` submitted in place of a value.

```console
$ obicfg unset sp4.CallerIDName --dry-run
plan:
  ~ VS_1_VP_1_L_4_.CallerIDName: 'Old name' -> ''
dry run: 1 reset(s) not sent
```

JSON output is the same shape as [`set`](#set).

## `apply`

Bring the device in line with a profile. Idempotent.

```sh
obicfg apply <PROFILE.toml> [--json] [-n|--dry-run] [--reboot|--no-reboot]
```

`--json` emits the same shape as [`set`](#set), plus a `profile` key naming
the profile, with planned resets folded into `plan[]`.

| Flag | Meaning |
|---|---|
| `-n`, `--dry-run` | Show the plan; send nothing |
| `--reboot` / `--no-reboot` | Override the profile's own `[after] reboot` |

Settings already at the requested value are skipped, so re-running after a
partial failure converges rather than thrashing. A `[require]` table pins the
profile to a model or firmware. Prompts before writing unless `-y`.

```console
$ obicfg apply trunks.toml --dry-run
profile: SIP trunks to a local PBX

plan:
  = VS_1_VP_1_L_2_.Enable already 'true'
  ~ VS_1_VP_2_SIP_.ProxyServer: '192.0.2.9' -> '192.0.2.10'
  ! VS_1_VP_1_L_4_.CallerIDName -> factory default

dry run: 1 change(s) not sent
```

`=` unchanged, `~` will change, `!` will be reset.

## `dump`

Back up the whole configuration to a directory.

```sh
obicfg dump <DIRECTORY> [--include-status] [--all] [--redact] [--force] [--no-raw]
```

| Flag | Meaning |
|---|---|
| `--include-status` | Also capture live status pages (excluded by default) |
| `--all` | Include read-only readouts. Noisy — some change every second |
| `--redact` | Blank passwords, MAC and serial |
| `--force` | Overwrite an existing backup in this directory |
| `--no-raw` | Write `snapshot.json` only, no per-page XML |

Writes `<page>.xml` for every *configuration* page plus `snapshot.json`.
Status and Setup Wizard pages are skipped unless `--include-status`, and
`--no-raw` skips the XML entirely. **Refuses to
overwrite an existing `snapshot.json` without `--force`** — a backup that eats
the previous backup is not a backup.

Snapshots hold **writable settings only**. Read-only readouts are excluded
because they are not settings and some of them (the WAN page carries a clock)
change every second, which would make every diff dirty.

```json
{
  "host": "192.0.2.50",
  "meta": {
    "taken": "2026-08-10T18:42:11-0400",
    "tool": "obicfg 0.1.0",
    "identity": {"ModelName": "OBi200", "SoftwareVersion": "3.2.2 (Build: 8680EX)"}
  },
  "pages": {
    "VS_1_VP_1_L_2_": {
      "title": "SP2 Service",
      "parameters": {
        "URI": {"hash": "9a71426c", "value": "200", "default": "", "is_default": false}
      }
    }
  }
}
```

A dump carries the device's MAC, serial and any stored SIP credentials. Keep
it out of public repositories, or use `--redact`.

## `diff`

Compare a snapshot against the device, or against another snapshot.

```sh
obicfg diff <SNAPSHOT> [--against <SNAPSHOT>] [--json]
```

`<SNAPSHOT>` may be a `snapshot.json` or the directory holding it. Without
`--against`, the comparison is made with the device as it is now.

**Exits 1 when differences are found** — that is a finding, not a failure.
Redacted values are skipped rather than reported as changes, with a note
saying how many.

```console
$ obicfg diff ./before
  ~ VS_1_VP_1_L_2_.CallerIDName
      - 'Old'
      + 'New'

1 difference(s)
```

```json
{
  "differences": [
    {"path": "VS_1_VP_1_L_2_.CallerIDName", "before": "Old", "after": "New"}
  ],
  "redacted_skipped": 0
}
```

## `status`

Device identity and per-service state.

```sh
obicfg status [--json] [--redact]
```

```console
$ obicfg status
ModelName        OBi200
SerialNumber     ...
SoftwareVersion  3.2.2 (Build: 8680EX)

SERVICE  ENABLED  ITSP  INBOUND ROUTE
-------  -------  ----  -------------
SP1      true     A     sp2(100)
```

```json
{"identity": {"ModelName": "OBi200", "...": "..."},
 "services": [{"service": "SP1", "enabled": "true", "profile": "A", "inbound": "sp2(100)"}]}
```

`--redact` masks the serial and MAC. Note `status` reports each service's
*configuration* — enabled, ITSP letter, inbound route — and **not** its
registration state; that lives in `telemetry`, under
`device.services[].status`.

`ModelName`'s factory *default* is `OBi100` on every model in the family; the
real model only ever appears as a current value. `obicfg` reads the effective
value, so this output is correct — but anything reading the raw default is not.

## `telemetry`

Live operational data: uptime, registration, RTP quality, line voltages, calls.

```sh
obicfg telemetry [--json] [--calls N] [--no-calls] [--redact] [--watch SECONDS]
```

| Flag | Meaning |
|---|---|
| `--calls N` | How many recent calls to include (default 20) |
| `--no-calls` | Skip call history — by far the largest fetch |
| `--redact` | Mask phone numbers, and the device serial and MAC. It does **not** mask the WAN address, or a device name embedded in an event line such as `From 'Front Desk' SP3(300)` — read the output before sharing it |
| `--watch SECONDS` | Repeat until interrupted |

Reads four endpoints: `DI_S_.xml` (identity, uptime, per-service
registration, WAN address and the device clock), `PI_FXS_1_Stats.xml`,
`VS_1_VP_1_L_1_Stats.xml` and `callhistory.xml`. A missing endpoint is
recorded in `errors` rather than raised — partial telemetry beats none.

```json
{
  "device": {
    "model": "OBi200", "firmware": "3.2.2 (Build: 8680EX)", "hardware": "1.4",
    "serial": "...", "mac": "...", "uptime_s": 234434, "reboots": 4,
    "system_time": "16:30:45     08/10/2026, Monday", "wan_ip": "192.0.2.50",
    "obitalk_status": "Backing Off (29)",
    "services": [
      {"sp": 1, "status": "Connected", "call_state": "0 Active Calls", "active_calls": 0}
    ]
  },
  "port": {
    "state": "On Hook", "loop_current_ma": 0.0, "vbat_v": 56.0,
    "tipring_v": 45.0, "last_caller": "--"
  },
  "quality": [
    {"sp": 1, "packets_sent": 136092, "packets_received": 138858,
     "bytes_sent": 21774720, "bytes_received": 22217280, "packets_lost": 0,
     "overruns": 0, "underruns": 0, "loss_percent": 0.0}
  ],
  "calls": [
    {"date": "8/10/2026", "time": "15:55:55", "direction": "inbound",
     "peer": "+15551234567", "from_terminal": "SP1", "to_terminal": "SP2",
     "connected": true, "answered": true, "ring_s": 0, "talk_s": 28,
     "events": [{"time": "15:55:55", "text": "From SP1(+15551234567)"}]}
  ],
  "calls_included": true,
  "errors": {}
}
```

A call the device could not classify comes back with `"direction":
"unknown"` and `peer`, `from_terminal`, `to_terminal` and `talk_s` all
`null` — an unanswered call, typically. `answered` mirrors `connected`.

`calls_included` is `false` when `--no-calls` was passed. Check it before
concluding a device has no call history: an empty `calls` list means one of
two very different things.

Notes on interpreting it:

- `loss_percent` is measured against `packets_received + packets_lost` — what
  *should* have arrived. A device cannot lose its own outbound stream.
- `loss_percent` is `null`, not `0`, when nothing has flowed.
- `status` of `"Registration Not Required"` is **healthy** — it is what an
  IP-authenticated trunk reports when it has no registrar. `"Service Not
  Configured"` usually means that service's `URI` is empty.
- `ring_s` and `talk_s` come from the gaps between call events, and tolerate a
  call that spans midnight.
- Call history is personal data. Use `--redact` before sharing.

## `reboot`

Reboot the device. Configuration survives; the unit is gone about 30 seconds.

```sh
obicfg reboot [--wait]
```

| Flag | Meaning |
|---|---|
| `--wait` | Block until the admin interface answers again (exit 1 if it does not) |

Prompts first unless `-y`. **A reboot drops calls in progress** — check
`telemetry` for active calls before running it. The unit often drops the
connection mid-reply as it goes down; that is a successful reboot, not an
error, and is treated as such.

## `probe`

Prove how this firmware decodes writes, on the device in front of you.

```sh
obicfg probe [--scratch <PATH>] [--json]
```

| Flag | Meaning |
|---|---|
| `--scratch` | A harmless writable parameter to test with (default `VS_1_VP_1_L_4_.CallerIDName`, SP4's caller ID name) |

**This writes to the device.** It sends a plain value, a value containing a
space, and a value containing `#` to the scratch parameter, reads each back,
then restores the original. Prompts first unless `-y`. Exit 3 if any case
fails to round-trip.

```console
$ obicfg probe
scratch parameter: VS_1_VP_1_L_4_.CallerIDName (currently '')
  ok   plain  'obicfgtest' -> 'obicfgtest'
  ok   space  'obicfg test' -> 'obicfg test'
  ok   hash   'obicfg#test' -> 'obicfg#test'
restored VS_1_VP_1_L_4_.CallerIDName to ''
```

Use it when a write fails oddly, or on an unfamiliar model. Pick a genuinely
cosmetic parameter — never one that carries routing or credentials.

---

## Profile format

```toml
name = "SIP trunks to the PBX"
description = "What this does and why."

[require]                   # optional; refuses to apply to the wrong device
model = "OBi2"              # prefix match against ModelName
firmware = "3.2"            # prefix match against SoftwareVersion
hardware = "1.4"
mac = "..."
serial = "..."

[settings]                  # dotted parameter paths -> desired values
"sp2.Enable"             = true
"sp2.X_UserAgentPort"    = 5061
"itsp.b.sip.ProxyServer" = "192.0.2.10"

[reset]                     # parameters to return to factory default
parameters = ["sp4.CallerIDName"]

[after]
reboot = false
```

Quote the dotted keys. Values may be strings, integers or booleans, and are
validated against the device's declared syntax before anything is sent.
Unknown top-level keys are rejected rather than ignored, so a typo in a
section name is an error and not a silently skipped setting.

TOML is read with the standard library's `tomllib` on Python 3.11+, and with a
bundled subset parser on older versions — no dependency either way.

## Config file format

`~/.config/obicfg/config.toml` (or `$XDG_CONFIG_HOME/obicfg/config.toml`, or
whatever `OBICFG_CONFIG` points at).

```toml
[device]
host      = "192.0.2.50"
username  = "admin"
password  = "admin"          # prefer OBI_PASSWORD_FILE
scheme    = "http"
port      = 80
transport = "paramlist"
timeout   = 15.0

[guard]
extra = []                   # additional protected patterns (fnmatch globs)
allow = []                   # drop a built-in rule by its exact pattern
detect_provisioned = true    # refuse writes to provisioned credentials
```

`obicfg` warns on stderr if this file contains a password and is readable by
anyone but you.

Note that `[guard]` can weaken the tool's own protections. If you did not
write this file yourself, read it before trusting the guard to stop anything.
