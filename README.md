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

```sh
pip install .
```

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

### Backups and drift

```sh
obicfg dump ./before-changes            # every page: raw XML + snapshot.json
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

Applying is idempotent — settings already correct are skipped, so re-running
after a partial failure converges instead of thrashing. A `[require]` table
pins a profile to a model or firmware so it cannot be applied to the wrong box
by a mistyped `--host`. See [`examples/`](examples/).

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
'${DSN}_1' is a provisioning macro, so it was pushed by a provisioning server
rather than typed; the OBiTALK provisioning cloud shut down in Nov 2024, so
anything it issued cannot be re-issued once overwritten.
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
whole page into one request.

There is an older form: `POST /result.html?hash=value`, with the value raw in
the query string. Nothing decodes it, so bytes are stored verbatim — send
`%20` and you get a literal `%20`. It is available as `--transport query` in
case a firmware ever ignores the first, and it carries two hard limits: a
space is not legal in an HTTP request line, and a `#` starts a URL fragment
and truncates everything after it. `obicfg` rejects such values up front
rather than letting the device mangle them.

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
| 1 | error, or `diff` found differences |
| 2 | bad usage |
| 3 | the device accepted a write and did not apply it |
| 4 | blocked by write protection |

## Development

```sh
pip install -e '.[dev]'
pytest
```

The test suite runs against an in-memory fake device and a real HTTP server on
localhost; no hardware needed. It includes a fake that answers `200` and
ignores the write, because that is what the real thing does and it is the
behaviour most worth testing against.

Nothing vendor-supplied is included in this repository — no firmware, no
stylesheets, no captured configuration. The fixtures are synthetic files
written to match the shape of the device's output.

## License

MIT. See [LICENSE](LICENSE).

Not affiliated with, endorsed by, or supported by Polycom, Obihai or Poly.
"OBi" and "OBiTALK" are their trademarks. This is an independent tool built by
reading what the device serves to its own admin interface.
