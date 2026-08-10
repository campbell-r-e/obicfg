---
name: obicfg
description: |
  Configure, back up, and monitor an OBi200-family VoIP ATA (OBi200/202/212,
  and much of the OBi100/110) using the `obicfg` CLI. Use when the user wants
  to read or change a setting on an OBi ("what's SP2 routing to?", "point the
  OBi at my PBX", "set the caller ID name"), set up or fix SIP trunks to a PBX,
  back up or restore a device's configuration, compare a device against a
  known-good snapshot, or check how it is actually behaving — registration
  state, packet loss, line voltages, recent calls, "why did that call drop".

  Do NOT use for other ATAs or SIP hardware (Grandstream, Cisco/Linksys SPA,
  Ooma), for configuring the PBX side (Asterisk/FreePBX dialplans, pjsip.conf),
  or for OBiTALK cloud/account questions — that service was retired in 2024 and
  nothing here can reach it.
---

# obicfg — drive an OBi ATA from the command line

`obicfg` is a CLI over the OBi's undocumented XML admin interface. This skill
sequences its commands for requests like *"point the OBi's SP2 at my PBX"* or
*"why is call audio breaking up?"*

**You carry no OBi logic.** Every safety property — parameter validation,
read-back verification, protection of irreplaceable credentials — is enforced
by `obicfg` itself. Your job is to sequence commands, parse their JSON, branch
on **exit codes**, and get explicit human sign-off before any write.

**Blast radius: this writes to a live telephone.** A bad write can stop
inbound calls, break 911 dialling, or destroy a credential that cannot be
re-issued. Treat every write as production.

## Golden rules (do not violate)

1. **Back up before the first write of a session.** Use a name that cannot
   collide with an earlier run — include the time, not just the date:
   `obicfg dump ./obi-backup-$(date +%Y-%m-%dT%H%M%S)`. `dump` refuses to
   overwrite an existing `snapshot.json`; if it does refuse, pick a new
   directory rather than reaching for `--force`, because the thing in the way
   is the restore point.
2. **Never change anything without a dry run the user has seen and approved.**
   `set --dry-run --json`, `unset --dry-run --json` and `apply --dry-run` all
   exist; use the one that matches. Show the user the plan in plain language
   and wait for explicit approval. "Fix my trunks" is not approval to write a
   specific value. This covers resets as much as writes: `unset` and a
   profile's `[reset]` table discard whatever is there now, which is
   destructive even though nothing new is being set.
3. **Branch on exit codes, not on prose.** See the table below. Exit 0 from a
   write does *not* by itself mean the setting changed — check `verified`.
4. **Never pass `--unsafe` on your own initiative.** Exit 4 means `obicfg`
   detected a provisioned credential that no server can re-issue. Stop, tell
   the user exactly what is protected and why, and proceed only if they
   instruct you to override *after* being told what is at risk.
   The guard can also be weakened from the config file (`[guard] allow`,
   `detect_provisioned = false`), so it is not an unconditional safety net.
   Do not assume it is armed on a machine you have not checked.
5. **Never report success without evidence.** A change is done when the JSON
   shows `"verified": true` for it and `failures` is empty. Quote that, not
   your expectation.
6. **Never put the password in the command line, and never print it.** argv is
   visible in `ps` to every user on the machine. Use `OBI_PASSWORD_FILE`,
   `OBI_PASSWORD`, or the config file. When triaging an authentication
   failure, do **not** `cat` the config file or the password file into the
   transcript — check that they exist and are readable, and ask the user to
   confirm the credential themselves.
7. **Never reboot without asking.** A reboot drops calls in progress. Check
   `telemetry` for active calls first, and say what you saw. This includes
   `set --reboot`, which reboots as a side effect of a write.
8. **Redact before sharing.** A dump carries the MAC, serial and any stored SIP
   credentials; call history is personal data. Both `dump --redact` and
   `telemetry --redact` mask the device serial and MAC, and `telemetry`
   additionally masks phone numbers — but **neither hides everything**, so
   read the output before passing it on. Never paste raw call history into a
   commit, an issue, or a published page.
9. **Never guess a parameter path.** `search` for it, `get` its current value,
   then act. A plausible-looking wrong path is the easiest way to write to the
   wrong service.
10. **`-y`/`--yes` is global, not per-prompt.** One `-y` disarms the
    confirmation on `unset`, `apply`, `reboot` and `probe` alike. On a
    non-interactive run the tool refuses to assume an answer and tells you to
    re-run with `--yes` — that message is not permission. Go back to the user,
    get approval for that specific action, and only then re-run with `-y`.
11. **Never pass `--no-verify`.** The read-back is the only evidence that a
    write took effect; without it success is unknowable, and rule 5 cannot be
    satisfied.

## Invoking

Use the `obicfg` console script if installed, otherwise `<repo>/bin/obicfg`,
which runs from a checkout with nothing installed. Python 3.8+, no dependencies.

Pass `--json` on every command that supports it and parse stdout:

| `--json` supported | no `--json` |
|---|---|
| `pages` `show` `search` `get` `set` `unset` `apply` `diff` `status` `telemetry` `probe` | `dump` `reboot` |

Connection settings come from flags, then environment, then
`~/.config/obicfg/config.toml`. Flags work before or after the subcommand.

```sh
export OBI_HOST=192.0.2.50 OBI_PASSWORD_FILE=~/.obi-pw
obicfg status --json
```

If the user has not said which device, and neither `OBI_HOST` nor a config file
provides one, **ask**. Do not scan the network for ATAs.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | success | For writes, still confirm `verified` is true |
| 1 | error (unreachable, bad credentials, no such parameter); `diff` found differences | Read the message; for `diff`, 1 is normal and means "there are changes" |
| 2 | bad usage | Fix the command; do not retry verbatim |
| 3 | the device accepted a write and did not apply it | See "when a write silently fails" below |
| 4 | blocked by write protection | **Stop.** Rule 4 |

## Workflow A — answer a question (read-only, no approval needed)

```sh
obicfg status --json                    # identity + per-service state
obicfg search 'InboundCallRoute' --json # find a setting without knowing its page
obicfg get sp2.X_InboundCallRoute --json
obicfg show sp2 --changed --json        # everything altered from factory on a page
obicfg pages --json                     # what this model exposes
```

Reads are safe and need no confirmation. Prefer `search` over guessing: it
matches parameter names *and* descriptions, so "the setting that controls
where inbound calls go" is findable.

## Workflow B — change a setting

1. **Find and confirm.** `search` for the parameter, then `get` its current
   value. Report what it is now.
2. **Back up** (once per session):
   `obicfg dump ./obi-backup-$(date +%Y-%m-%dT%H%M%S)`.
3. **Dry run**: `obicfg set <path>=<value> --dry-run --json`. Every entry has
   `noop: true` (already correct) or `old`/`new`.
4. **Show the user** the plan and what it will do in their terms — "this stops
   inbound calls ringing the handset and sends them to the PBX instead" — and
   **wait for approval**.
5. **Apply**: `obicfg set <path>=<value> --json`.
6. **Verify**: confirm `failures` is empty and every entry in `applied` has
   `"verified": true`. Report exactly that.

If a run is interrupted between step 5 and step 6, do not re-send. Read the
current state back with `get`, or `diff` against the backup from step 2, and
report what actually happened before doing anything else.

If a value contains spaces, `#`, or braces, pass it as a single shell-quoted
argument; the default transport carries all of them. For very long values
(DigitMap, OutboundCallRoute), write the value to a file and use
`obicfg set phone.OutboundCallRoute=@route.txt` so the shell cannot mangle it.

Settings on the same page are written in one request, so batch related changes
into a single `set` rather than several.

## Workflow C — apply a profile

Profiles are TOML files describing desired state; applying is idempotent.

```sh
obicfg apply ./trunks.toml --dry-run    # always first
obicfg apply ./trunks.toml --yes        # only after approval
```

Prefer a profile over a long `set` when there is more than a handful of
settings, when the same setup will be repeated on another unit, or when the
user will want it version-controlled. Add a `[require]` table pinning model or
firmware so it cannot be applied to the wrong box. See
`references/recipes.md` for the file format and worked examples.

`apply` prompts before writing; `--yes` skips the prompt, so only pass it once
the human has approved the plan you showed them.

**If a profile exists for this device, it is the record of truth.** After an
ad-hoc `set`, offer to fold the change into the profile. Otherwise the
committed profile quietly stops describing the device, and the next `apply`
reverts a change nobody remembers making.

## Workflow D — put something back to factory default

```sh
obicfg unset sp4.CallerIDName --dry-run --json   # always first
obicfg unset sp4.CallerIDName --json --yes       # only after approval
```

The dry run shows what the value would revert *to*. Treat this with the same
care as a write: whatever is there now is discarded, and if it was not in a
backup it is gone.

## Sample JSON

The shapes you will be parsing. Full reference in
[`docs/COMMANDS.md`](../../docs/COMMANDS.md).

`set` / `unset` / `apply`:

```json
{
  "dry_run": false,
  "verified": true,
  "plan":    [{"path": "VS_1_VP_1_L_2_.URI", "old": "200", "new": "201", "noop": false}],
  "applied": [{"path": "VS_1_VP_1_L_2_.URI", "value": "201",
               "verified": true, "observed": "201"}],
  "failures": []
}
```

`failures` non-empty (and exit 3) looks like:

```json
{"failures": [{"path": "VS_1_VP_1_L_2_.URI", "wanted": "201", "observed": "200"}]}
```

`telemetry` (abridged — note the nesting, and that `loss_percent` is `null`
rather than `0` when nothing has flowed):

```json
{
  "device": {"model": "OBi200", "uptime_s": 234434, "reboots": 4,
             "services": [{"sp": 1, "status": "Connected", "active_calls": 0}]},
  "port":   {"state": "On Hook", "loop_current_ma": 0.0, "vbat_v": 56.0},
  "quality":[{"sp": 1, "packets_received": 138858, "packets_lost": 0,
              "overruns": 0, "underruns": 0, "loss_percent": 0.0}],
  "calls":  [{"date": "8/10/2026", "time": "15:55:55", "direction": "inbound",
              "peer": "+15551234567", "connected": true, "ring_s": 0, "talk_s": 28}],
  "errors": {}
}
```

`show --json` gives `options` for any enumerated parameter — the device's own
list of legal values, which is what to check against after an exit 3.

## Workflow E — backup and drift

```sh
obicfg dump ./obi-2026-08-10                # raw XML per page + snapshot.json
obicfg diff ./obi-2026-08-10                # snapshot vs the device now
obicfg diff ./jan --against ./feb           # two snapshots
```

`diff` exits 1 when it finds differences — that is a finding, not a failure.
Snapshots hold writable settings only, so a clean diff means the configuration
is unchanged.

## Workflow F — troubleshoot behaviour

```sh
obicfg telemetry --json                     # everything
obicfg telemetry --no-calls --json          # skip the largest fetch
obicfg telemetry --redact --calls 10        # sharing-safe
```

Map the complaint to the evidence:

| Complaint | Look at |
|---|---|
| "calls don't come in" | service `status`, and the `X_InboundCallRoute` of the service they arrive on |
| "audio breaks up / robotic" | `quality[].loss_percent`, `overruns`, `underruns` per service |
| "no dial tone", "phone dead" | `port.state`, `loop_current_ma`, `vbat_v` |
| "the call dropped" | `calls[]` — `answered`, `ring_s`, `talk_s`, and the raw `events` |
| "did it reboot?" | `device.uptime_s` and `device.reboots` |

## When a write silently fails (exit 3)

`result.html` returns HTTP 200 for writes it discards, which is why `obicfg`
reads every write back. Exit 3 means the device confirmed nothing changed.
Do **not** retry the identical command. In order:

1. Read `failures[].observed` — what the device actually holds now.
2. Check the value against the page: `obicfg show <page> --json` gives
   `options` for enumerations and the declared type.
3. If the value is legal and still will not stick, try `--transport query`,
   the older encoding. Note it cannot carry a space or `#`.
4. If it still fails, report the failure plainly with the observed value.
   Do not describe the setting as changed.

## Things that look broken but are not

- **"Registration Not Required"** is a *healthy* service state. It is what an
  IP-authenticated trunk reports when it has no registrar. Do not "fix" it.
- **"Service Not Configured"** usually means the service's `URI` is empty —
  that field is what brings a registration-less service up.
- **`ModelName`'s factory default is `OBi100`** on every model in the family.
  Read the effective value (which `obicfg` already does), never the default.
- **A `${...}` value** is a provisioning macro, not a corrupted setting.
- **Read-only parameters** are absent from snapshots on purpose. That is not a
  gap in the backup; they are readouts, not settings.

## Never do these

- Never run the device's Setup Wizard, or re-enable auto-provisioning. Both
  rewrite configuration in bulk against a cloud that no longer exists.
- Never factory-reset an OBi to "start clean". On a unit that was ever
  provisioned, the reset is unrecoverable.
- Never write to a service whose credentials `obicfg` reports as provisioned.
- Never suggest OBiTALK as a solution to anything; it was retired in Nov 2024.
- Never commit a dump, a snapshot, or call history to a repository.

## Reference

`references/recipes.md` — page aliases, the SP/ITSP numbering trap, common
parameter paths, the profile format, and worked setups (PBX trunks,
point-to-point SIP, emergency-call routing).
