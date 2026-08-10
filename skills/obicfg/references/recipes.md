# obicfg reference

Page aliases, the naming traps, common parameter paths, the profile format,
and worked setups. Everything here is verified against an OBi200 on firmware
3.2.2 (Build 8680EX).

## The numbering trap

In a page name like `VS_1_VP_1_L_2_`:

- the **`VP` index is the ITSP profile** (A, B, C, D) — the SIP/RTP/tone
  settings;
- the **`L` index is the service** (SP1–SP4) — the trunk you dial through.

So **SP2 is `VS_1_VP_1_L_2_`**, while **ITSP Profile B is `VS_1_VP_2_`**. A
service points at a profile through its `X_ServProvProfile` field, which holds
a letter (`"A"`, `"B"`, …). Getting this backwards writes to the wrong thing
while looking entirely reasonable.

## Aliases

| Alias | Page | What lives there |
|---|---|---|
| `sp1`–`sp4` | `VS_1_VP_1_L_1_`–`_L_4_` | per-service: enable, URI, routing, auth, SIP port |
| `itsp.a`–`itsp.d` | `VS_1_VP_1_`–`VS_1_VP_4_` | profile-wide behaviour |
| `itsp.a.sip`–`itsp.d.sip` | `VS_1_VP_n_SIP_` | proxy, registrar, outbound proxy, transport |
| `itsp.a.rtp`–`itsp.d.rtp` | `VS_1_VP_n_RTP_` | RTP ports, DTMF, codecs in use |
| `phone`, `fxs` | `VS_1_X_FXS_1_` | the analogue port: dial plan, routing, primary line |
| `gateways`, `gw` | `VS_1_X_GW_` | gateways and trunk groups |
| `aa` | `VS_1_X_AA_1_` | auto attendant |
| `codec.a`, `codec.b` | `VS_1_CODEC_1_`, `_2_` | codec profiles |
| `star.a`, `star.b` | `VS_1_X_STAR_1_`, `_2_` | star-code profiles |
| `ring.a`, `ring.b` | `VS_1_VP_1_L_1_R_`, `_L_2_R_` | ring profiles |
| `digitmaps` | `DIGITMAPS_` | user-defined digit maps |
| `speeddial` | `SPEEDDIAL_` | speed dial slots |
| `wan` | `DI_NS_` | WAN addressing, NTP, time zone |
| `status`, `system` | `DI_S_` | identity, uptime, per-service status |
| `admin` | `DM_MISC_` | device admin, syslog, web port |
| `wifi` | `USB_WIFI_` | USB Wi-Fi (OBi200 with dongle) |
| `provisioning` | `DM_S_` | auto-provisioning — **protected, do not write** |
| `wizard` | `SetupWizard` | **protected, do not use** |

Raw page names always work. `obicfg pages` lists what a given model actually
has; `obicfg search <regex>` finds a parameter without knowing either name.

## Parameters worth knowing

Per service (`sp1`…`sp4`):

| Parameter | Meaning |
|---|---|
| `Enable` | whether the service runs at all |
| `X_ServProvProfile` | which ITSP profile it uses — `"A"`…`"D"` |
| `X_RegisterEnable` | `false` for an IP-authenticated trunk with no registrar |
| `URI` | the identity/extension this service answers as. An empty URI is why a registration-less service reads "Service Not Configured" |
| `AuthUserName`, `AuthPassword` | credentials — **often provisioned; check before touching** |
| `X_UserAgentPort` | this service's own SIP port. Two trunks to the same PBX need different ports |
| `X_InboundCallRoute` | where calls arriving on this service go: `ph` (the handset), `sp1`…`sp4` (out another service), `sp2(100)` (out SP2 to extension 100), `aa` (auto attendant) |
| `CallerIDName` | cosmetic outbound caller name |

Per ITSP profile (`itsp.b.sip`…):

| Parameter | Meaning |
|---|---|
| `ProxyServer`, `ProxyServerPort` | where SIP goes |
| `ProxyServerTransport` | `UDP`, `TCP` or `TLS` |
| `RegistrarServer`, `OutboundProxy` | when they differ from the proxy |

The analogue port (`phone`):

| Parameter | Meaning |
|---|---|
| `OutboundCallRoute` | the dial plan: which digits go out which service |
| `PrimaryLine` | default outbound service. Its values contain a space (`"SP3 Service"`), so prefer an explicit `:spN` rule in `OutboundCallRoute` |
| `DigitMap` | what counts as a complete number |

## Call-route syntax

`X_InboundCallRoute` and `OutboundCallRoute` are rule lists in braces,
evaluated in order, first match wins:

```
{(911):sp1},{**0:aa},{(<**2:>(Msp2)):sp2},{(Mpli):pli}
```

- `{(911):sp1}` — dial 911, go out SP1. **Put emergency rules first** so they
  match before anything that could send them to a PBX that might be down.
- `{(<**2:>(Msp2)):sp2}` — strip the `**2` prefix, match SP2's digit map,
  send out SP2.
- `{(Mpli):pli}` — anything matching the primary line's digit map goes to the
  primary line. Usually the last rule.

The stock route sends unmatched numbers to `pp` (OBiTALK), where they now
vanish silently. Replace that rule on any unit you are configuring fresh.

## Profile format

```toml
name = "SIP trunks to the PBX"
description = "What this does and why."

[require]                  # optional; refuses to apply to the wrong device
model = "OBi2"             # prefix match against ModelName
firmware = "3.2"           # prefix match against SoftwareVersion

[settings]                 # dotted parameter paths -> desired values
"sp2.Enable"                = true
"sp2.X_UserAgentPort"       = 5061
"sp2.X_InboundCallRoute"    = "sp1"
"itsp.b.sip.ProxyServer"    = "192.0.2.10"

[reset]                    # back to factory default
parameters = ["sp4.CallerIDName"]

[after]
reboot = false
```

Quote the dotted keys. Values may be strings, integers or booleans; they are
validated against the device's declared syntax before anything is sent.

## Worked setup: two trunks to a PBX

The device and the PBX see each other as one IP address, so the PBX cannot
tell "a call for the outside world" from "a call for the handset" by source.
Encode the direction in *which trunk* is used:

- **SP2** → outbound. `X_InboundCallRoute = "sp1"`: anything the PBX sends here
  leaves via SP1.
- **SP3** → inbound. `X_InboundCallRoute = "ph"`: anything the PBX sends here
  rings the analogue phone.
- Different `X_UserAgentPort` on each (5061, 5062).
- `X_RegisterEnable = false` on both; authenticate by IP so no password
  crosses the LAN. Healthy status is then "Registration Not Required".
- Give each a `URI` (e.g. `200`, `201`) — without one they will not come up.

Then the PBX dials one endpoint to get out and the other to ring the phone.
See `examples/pbx-trunks.toml` in the repository for the complete file.

## Worked setup: two OBis, no server

For a LAN with no internet: point each unit's ITSP profile `ProxyServer` at
the *other* unit's IP, `X_RegisterEnable = false`, give each a `URI` (`100`,
`200`), and set `X_InboundCallRoute = "ph"`. Dial the other's URI from the
handset. See `examples/p2p-direct.toml`.

This is plain SIP, not OBiTALK — OBiTALK's number-based dialling was always
cloud-mediated and has no raw-IP form, so it cannot work offline.

## Emergency calling

Put `{(911):sp1}` (or whichever service reaches a carrier) **first** in
`phone.OutboundCallRoute`, ahead of any rule that routes through a PBX, so
emergency dialling survives the PBX being down. Say plainly that VoIP 911 is
unreliable regardless — many services do not deliver location, and some do not
carry 911 at all.

## Confirming the write transport

`obicfg probe --scratch <path>` writes a plain value, a value containing a
space, and a value containing `#` to a nominated harmless parameter, reads
each back, and restores the original. Use it when a write fails oddly, or on
an unfamiliar model. It **does write to the device** — get approval first, and
pick a genuinely cosmetic parameter (the default is SP4's caller ID name).
