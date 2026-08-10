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

Every alias, in full. Several pages have two names because both readings are
natural; they resolve to the same page.

| Alias | Page | What lives there |
|---|---|---|
| `sp1` | `VS_1_VP_1_L_1_` | SP1 service: enable, URI, call routing, auth, its own SIP port |
| `sp2` | `VS_1_VP_1_L_2_` | SP2 service (same fields as SP1) |
| `sp3` | `VS_1_VP_1_L_3_` | SP3 service (same fields as SP1) |
| `sp4` | `VS_1_VP_1_L_4_` | SP4 service (same fields as SP1) |
| `itsp.a` | `VS_1_VP_1_` | ITSP Profile A: profile-wide call behaviour |
| `itsp.b` | `VS_1_VP_2_` | ITSP Profile B |
| `itsp.c` | `VS_1_VP_3_` | ITSP Profile C |
| `itsp.d` | `VS_1_VP_4_` | ITSP Profile D |
| `itsp.a.sip` | `VS_1_VP_1_SIP_` | Profile A SIP: proxy, registrar, outbound proxy, transport |
| `itsp.b.sip` | `VS_1_VP_2_SIP_` | Profile B SIP |
| `itsp.c.sip` | `VS_1_VP_3_SIP_` | Profile C SIP |
| `itsp.d.sip` | `VS_1_VP_4_SIP_` | Profile D SIP |
| `itsp.a.rtp` | `VS_1_VP_1_RTP_` | Profile A RTP: port range, DTMF method, codec selection |
| `itsp.b.rtp` | `VS_1_VP_2_RTP_` | Profile B RTP |
| `itsp.c.rtp` | `VS_1_VP_3_RTP_` | Profile C RTP |
| `itsp.d.rtp` | `VS_1_VP_4_RTP_` | Profile D RTP |
| `tone.a` | `VS_1_VP_1_T_` | Tone Profile A: call-progress tones (dial, busy, ringback) |
| `tone.b` | `VS_1_VP_2_T_` | Tone Profile B |
| `ring.a` | `VS_1_VP_1_L_1_R_` | Ring Profile A: ring cadences and distinctive ring rules |
| `ring.b` | `VS_1_VP_1_L_2_R_` | Ring Profile B |
| `codec.a` | `VS_1_CODEC_1_` | Codec Profile A: which codecs are offered, in what order |
| `codec.b` | `VS_1_CODEC_2_` | Codec Profile B |
| `phone` | `VS_1_X_FXS_1_` | the analogue PHONE port: dial plan, outbound routing, primary line |
| `fxs` | `VS_1_X_FXS_1_` | same page as `phone` |
| `gateways` | `VS_1_X_GW_` | gateways and trunk groups |
| `gw` | `VS_1_X_GW_` | same page as `gateways` |
| `aa` | `VS_1_X_AA_1_` | the auto attendant |
| `autoattendant` | `VS_1_X_AA_1_` | same page as `aa` |
| `pbx` | `VS_1_X_PBX_` | OBiPLUS, the built-in mini-PBX |
| `obiplus` | `VS_1_X_PBX_` | same page as `pbx` |
| `p2p` | `VS_1_X_P2P_1_` | OBiTALK service settings (the cloud is retired; kept for reference) |
| `obitalk` | `VS_1_X_P2P_1_` | same page as `p2p` |
| `star.a` | `VS_1_X_STAR_1_` | Star Code Profile A: what `*xx` codes do |
| `star.b` | `VS_1_X_STAR_2_` | Star Code Profile B |
| `speeddial` | `SPEEDDIAL_` | speed-dial slots |
| `digitmaps` | `DIGITMAPS_` | user-defined digit maps |
| `pagegroups` | `VS_1_X_PageGroups` | paging groups |
| `bt1` | `VS_1_X_BT_1_` | Bluetooth pairing 1 (OBi200 with a USB dongle) |
| `bt2` | `VS_1_X_BT_2_` | Bluetooth pairing 2 |
| `wan` | `DI_NS_` | WAN addressing, NTP, time zone |
| `status` | `DI_S_` | identity, uptime, per-service status (read-only) |
| `system` | `DI_S_` | same page as `status` |
| `admin` | `DM_MISC_` | device admin: web port, syslog, admin password |
| `provisioning` | `DM_S_` | auto-provisioning — **protected, do not write** |
| `wifi` | `USB_WIFI_` | USB Wi-Fi settings |
| `wizard` | `SetupWizard` | the Setup Wizard — **protected, do not use** |
| `sp1.stats` | `VS_1_VP_1_L_1_Stats` | RTP counters per service (read-only) |
| `phone.stats` | `PI_FXS_1_Stats` | analogue port hardware state: hook, loop current, VBAT (read-only) |

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
