# obicfg

Configure an OBi200-family VoIP ATA from the command line, safely and
repeatably.

These devices only ever shipped one management interface: a frame-based web
UI from 2013 that renders XML through an XSL stylesheet, has no API, no CLI,
no SSH, and no documentation for any of it. Every setting is addressed by an
undocumented eight-hex-digit hash. Configuring one by hand means clicking
through forty-two pages; configuring twenty means clicking through forty-two
pages twenty times.

`obicfg` reads the device's own pages to discover what exists, addresses
settings by name, writes many at once, and — the part that matters — reads
every write back, because the device returns `HTTP 200` whether or not it
applied anything.

```console
$ obicfg -H 192.0.2.50 status
ModelName        OBi200
SoftwareVersion  3.2.2 (Build: 8680EX)

SERVICE  ENABLED  ITSP  INBOUND ROUTE
-------  -------  ----  -------------
SP1      true     A     ph
SP2      true     B     sp1

$ obicfg set sp2.X_InboundCallRoute=sp1 sp2.X_UserAgentPort=5061
plan:
  ~ VS_1_VP_1_L_2_.X_InboundCallRoute: 'ph' -> 'sp1'
  ~ VS_1_VP_1_L_2_.X_UserAgentPort: '5060' -> '5061'

  ok   VS_1_VP_1_L_2_.X_InboundCallRoute = 'sp1'
  ok   VS_1_VP_1_L_2_.X_UserAgentPort = '5061'
```

**Status:** tested against an OBi200, hardware 1.4, firmware 3.2.2 (Build
8680EX) — the last release Polycom shipped. It should work on the OBi202 and
OBi212, which run the same firmware line and the same admin interface, and
much of it will work on the older OBi100/110; page discovery is dynamic, so
an unfamiliar model degrades to "some aliases are missing" rather than
breaking. Reports from other models are welcome.

## Install

Requires Python 3.8 or newer. There are no dependencies — not as a boast, but
because the machine that can reach an ATA is often a router, a NAS or a
fifteen-year-old netbook where `pip install` is a whole project of its own.
The full suite passes on 3.8, 3.9, 3.10 and 3.11+; on anything before 3.11 the
bundled TOML parser takes over from `tomllib`, and that path is covered too.

```sh
pip install .
```

`python3 -m obicfg` works too, if you would rather not rely on the console
script being on `PATH`.

Or skip installing altogether. Copy the repository to whatever box is on the
right network and run it in place:

```sh
./bin/obicfg --host 192.0.2.50 status
```

Tested on Linux, macOS and the BSDs. Nothing in it is platform-specific.

## Connecting

In order of precedence: flags, environment, config file.

```sh
obicfg --host 192.0.2.50 --password-file ~/.obi-pw status
OBI_HOST=192.0.2.50 OBI_PASSWORD=admin obicfg status
```

Connection and safety flags work before or after the subcommand, so both
`obicfg --host X status` and `obicfg status --host X` do the same thing.

Or put it in `~/.config/obicfg/config.toml` once — see
[`examples/config.toml`](examples/config.toml). Prefer `--password-file` or
`OBI_PASSWORD_FILE` over `--password`: an argument is visible in `ps` output
to every user on the machine.

## Finding things

You do not need to know that SP2's inbound routing lives on a page called
`VS_1_VP_1_L_2_`.

```sh
obicfg pages                       # what this device has, from its own menu
obicfg search InboundCallRoute     # find a setting by name or description
obicfg show sp2 --changed          # what has been altered from factory on a page
obicfg get sp2.X_InboundCallRoute
```

Aliases cover the common pages (`sp1`–`sp4`, `itsp.a`–`itsp.d`, `itsp.b.sip`,
`phone`, `gateways`, `codec.a`, `wan`, …), raw page names always work, and
`search` finds anything either way.

> The numbering is a trap worth knowing about. In `VS_1_VP_1_L_2_`, the `VP`
> index is the **ITSP profile** (A–D) and the `L` index is the **service**
> (SP1–SP4). So SP2 is `VP_1_L_2_`, while ITSP Profile B is `VP_2_`. The
> aliases exist mostly to stop you having to hold that in your head.

## Changing things

```sh
obicfg set sp2.Enable=true sp2.X_UserAgentPort=5061      # verified after writing
obicfg set phone.OutboundCallRoute=@route.txt            # long values from a file
obicfg unset sp4.CallerIDName                            # back to factory default
```

Values are checked against the device's own declared syntax — enumerations,
integer ranges, string lengths — *before* anything is sent, because a rejected
write looks exactly like a successful one from the outside.

Settings on the same page go out in a single request, which is what the web UI
does when you press Submit. Reconfiguring twenty settings is a couple of round
trips, not twenty.

### Live events

Everything above polls. The device will also *push* — a plain UDP syslog
stream carrying call setup, answer, teardown and registration changes as they
happen, which is the only real-time interface it has.

```sh
obicfg listen --setup                 # print the writes that point it at you
obicfg listen --calls-only --json     # then watch
```

A device sends syslog to exactly one address, so if something already collects
it, use [`contrib/obicaller.py`](contrib/obicaller.py) — a standalone,
dependency-free daemon that relays each datagram on to the existing consumer
while announcing callers, logging, or running a hook. Service units and deployment notes are in [`deploy/`](deploy/).

`contrib/obi-syslog-pg.py` will store the stream in PostgreSQL, loading it
with `COPY` through `psql` so no database driver is needed. See
[`deploy/`](deploy/).

The idea of using this stream as a live event source is not mine: it comes
from [obicaller](https://github.com/YoRyan/obicaller) by Ryan Young, a
public-domain talking caller-ID daemon. No code is reused; the insight is
credited in full at the top of `contrib/obicaller.py`.

### Backups and drift

```sh
obicfg dump ./before-changes            # config pages: raw XML + snapshot.json
obicfg diff ./before-changes            # what has changed since, on the device
obicfg diff ./jan --against ./feb       # or between two snapshots
```

Snapshots hold writable settings only. Read-only readouts are excluded on
purpose: the WAN page carries a clock that ticks every second, and including
it would make every diff dirty enough to hide the one line you needed to see.

A dump contains the device's MAC, serial and any stored SIP credentials. Use
`--redact` before putting one anywhere public; `diff` skips redacted values
rather than reporting them as changes.

### Profiles

Describe the state you want, apply it as often as you like:

```sh
obicfg apply examples/pbx-trunks.toml --dry-run
obicfg apply examples/pbx-trunks.toml
```

Worked examples: [`examples/pbx-trunks.toml`](examples/pbx-trunks.toml) (two
SIP trunks to a PBX) and [`examples/p2p-direct.toml`](examples/p2p-direct.toml)
(two OBis calling each other with no server at all).

Applying is idempotent — settings already correct are skipped, so re-running
after a partial failure converges instead of thrashing. A `[require]` table
pins a profile to a model or firmware so it cannot be applied to the wrong box
by a mistyped `--host`. See [`examples/`](examples/).

## Telemetry

Configuration is what the device should do; telemetry is what it is actually
doing.

```console
$ obicfg telemetry --calls 4
OBi200  fw 3.2.2 (Build: 8680EX)  up 2d 17h 34m  (4 reboots)
device clock: 16:57:42     08/10/2026, Monday

SVC  STATUS                     CALLS  TX PKTS  RX PKTS  LOST  LOSS
---  -------------------------  -----  -------  -------  ----  -----  -
SP1  Connected                  0      136092   138858   0     0.00%
SP2  Registration Not Required  0      138574   136191   0     0.00%
SP4  Service Not Configured     0      0        0        0            !

phone port: On Hook, loop 0 mA, VBAT 56 V, tip-ring 45 V

WHEN                DIRECTION  PEER          PATH      ANSWERED  RING  TALK
------------------  ---------  ------------  --------  --------  ----  ----
8/10/2026 15:55:55  inbound    +xxxxxxxxx30  SP1->SP2  yes       0s    28s
```

Four endpoints carry all of it: device status (including the WAN address and
the device's own clock), the analogue port's electrical state, RTP counters
per service, and call detail records.
`--json` emits the parsed structures for a collector to store; `--watch 30`
repeats until interrupted; `--redact` masks phone numbers, which is worth
remembering because call history is the one part of an ATA's state that is
personal data.

Three things the parsing gets right that a naive reading would not:

- **`callhistory.xml` is not in the admin menu.** Nothing that discovers
  pages by crawling the UI will find it, and it has a schema of its own —
  an event log per call rather than a settings page. Ring and talk time come
  from the gaps between those events, including across midnight.
- **The call-quality page repeats `<object name="RTP Statistics">` once per
  service**, all with identical parameter names. The block boundaries carry
  the meaning, so grouping by name would fold SP4's counters onto SP1's.
- **"Registration Not Required" is healthy**, not an error — it is what an
  IP-authenticated trunk reports when there is no registrar to talk to.
  "Service Not Configured" usually means the URI is empty.

Packet loss is reported against packets that should have arrived
(`received + lost`), not against packets sent; a device cannot lose its own
outbound stream.

## Write protection

By default `obicfg` refuses to overwrite settings that cannot be recovered.

The OBiTALK provisioning cloud was retired in November 2024. Anything it
pushed onto a device — most famously a Google Voice binding, but equally an
ITSP account a provider set up for a customer — now exists in exactly one
place: the flash on that one unit. There is no longer a server that can
re-issue it. Overwrite it and the credential is gone, and so is the phone
number attached to it.

Rather than assuming which profile is precious, `obicfg` detects it.
Provisioning macros survive verbatim in the stored configuration, so a
parameter still holding `${DSN}_1` is direct evidence of a value no human
typed and nothing can regenerate. Credentials sharing a page with one are
protected by association.

```console
$ obicfg set sp1.AuthUserName=someone
obicfg: refusing to write VS_1_VP_1_L_1_.AuthUserName: its current value
'${DSN}_1' is a provisioning macro and not the factory default, so it was
pushed by a provisioning server rather than typed; the OBiTALK provisioning
cloud shut down in Nov 2024, so anything it issued cannot be re-issued once
overwritten.
  Back up first (obicfg dump ./before), and re-run with --unsafe if you are
  certain.
```

Auto-provisioning and the Setup Wizard are blocked too — both rewrite
configuration in bulk. `--unsafe` disables all of it; the `[guard]` table adds
or removes rules. On a factory-reset unit, or one only ever configured by
hand, nothing here will fire.

## How writes actually work

Worth understanding, because it explains the tool's two odd corners.

The admin UI does not POST a form the way you would expect. `default.xsl`
builds `hash=encodeURIComponent(value)` pairs in JavaScript, joins them with
`&`, puts the whole string into one hidden field called `ParameterList`, and
POSTs *that* to `result.html`. The value therefore crosses the wire
percent-encoded **twice** — once by the page's script, once by the browser's
form serialiser — and the device undoes both.

`obicfg` reproduces that exactly (`--transport paramlist`, the default), which
is why it can write values containing spaces and `#`, and why it can batch a
whole page into one request. That is not a deduction from reading the
JavaScript — `obicfg probe` round-trips a plain value, a value with a space
and a value with a `#` through a scratch parameter and reads each back. On an
OBi200 running 3.2.2, all three survive intact.

There is an older form: `POST /result.html?hash=value`, with the value raw in
the query string. Nothing decodes it, so bytes are stored verbatim — send
`%20` and you get a literal `%20`. It is available as `--transport query` in
case a firmware ever ignores the first, and it carries hard limits. A space is not legal in an HTTP request line; a
`#` starts a URL fragment and truncates everything after it; and an `&` ends
the assignment, so the tail of the value is read as a **second** parameter and
written to whatever it happens to name — a write nobody asked for, which the
read-back on the intended parameter cannot see. `=` and `+` are refused for
related reasons. `obicfg` rejects all of them up front rather than letting the
device mangle the value or scatter it.

This is also why the tool speaks HTTP through the standard library rather than
a friendlier client. A call route like `{(<**1:>(Msp1)):sp1}` is completely
ordinary on an OBi, and any well-behaved HTTP library will percent-encode
those braces on the way out. The device then stores the escapes, and the phone
silently stops routing calls.

Two more sharp edges the tool handles for you:

- **`default` is a reserved value.** The UI submits the literal string
  `default` to mean "reset this to factory", so no parameter can hold it as a
  value. `obicfg` rejects it with an explanation and points you at `unset`.
- **`ModelName`'s factory default is `OBi100`** on every model in the family.
  The real model only ever appears in the `current` attribute. Read the
  default and every device claims to be an OBi100.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | success |
| 1 | error; also `diff` finding differences, or `search` matching nothing |
| 2 | bad usage |
| 3 | the device accepted a write and did not apply it |
| 4 | blocked by write protection |

## Documentation

- **[`docs/COMMANDS.md`](docs/COMMANDS.md)** — every command, every flag, and
  the exact shape of every `--json` output.
- **[`skills/obicfg/`](skills/obicfg/)** — an agent skill for driving this tool
  from Claude Code, with the safety sequencing spelled out. Install it with
  `ln -s "$PWD/skills/obicfg" ~/.claude/skills/obicfg`.
- **[`skills/obicfg/references/recipes.md`](skills/obicfg/references/recipes.md)**
  — page aliases, the SP/ITSP numbering trap, call-route syntax, and worked
  setups. Useful whether or not you use the skill.

## Development

```sh
pip install -e '.[dev]'
pytest
```

There is also a hardware sweep, [`tests/live/sweep.sh`](tests/live/sweep.sh),
which exercises every feature — including writes, profiles, resets and the
transport probe — against a real device. Every write it makes targets a
parameter nothing uses, and it finishes by proving the device is
byte-identical to the backup it took at the start. Read its header before
running it: it tells you how to check that the scratch targets are inert on
*your* unit.

Coverage is gated at **100%** and the suite fails below it. That is not
box-ticking: this tool writes to a telephone and reports success or failure on
the strength of a read-back, so an untested branch is a branch that could
claim a change that never happened.

The test suite runs against an in-memory fake device and a real HTTP server on
localhost; no hardware needed. It includes a fake that answers `200` and
ignores the write, because that is what the real thing does and it is the
behaviour most worth testing against.

Nothing vendor-supplied is included in this repository — no firmware, no
stylesheets, no captured configuration. The fixtures are synthetic files
written to match the shape of the device's output.

## Acknowledgements

The live-event approach in `contrib/obicaller.py` — treating the device's UDP
syslog stream as a real-time call feed — comes from
[**obicaller**](https://github.com/YoRyan/obicaller) by **Ryan Young**, a
public-domain talking caller-ID daemon for the OBi200, archived in 2022. No
code from it is reused here; it is a shell script and this is Python. The name
is kept in tribute.

## License

MIT. See [LICENSE](LICENSE).

Not affiliated with, endorsed by, or supported by Polycom, Obihai or Poly.
"OBi" and "OBiTALK" are their trademarks. This is an independent tool built by
reading what the device serves to its own admin interface.
