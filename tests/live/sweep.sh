#!/bin/sh
# Exercise every feature against a REAL device, safely.
#
# The unit test suite runs against fakes. This runs against hardware, because
# some of what this tool asserts -- that the device decodes a value containing
# a space, that it silently drops writes it dislikes, that a page absent from
# the menu is still served -- can only be established by asking a real OBi.
#
# Safety, in order of importance:
#   * Every write targets a parameter nothing uses. SP4 is "Service Not
#     Configured" and ITSP Profile D is referenced by no service, so writes
#     there cannot affect a call. CHECK THAT IS TRUE OF YOUR UNIT FIRST:
#         obicfg get sp1.X_ServProvProfile sp2.X_ServProvProfile \
#                    sp3.X_ServProvProfile sp4.X_ServProvProfile
#     If any service points at profile D, or SP4 is in service, edit the
#     scratch targets below before running.
#   * A full backup is taken first, and the run ends by proving the device is
#     byte-identical to it. A failure at the last step means something was
#     left changed -- restore from $D/before.
#   * Nothing here reboots the device.
#
# Usage:
#   OBI_HOST=192.0.2.50 OBI_PASSWORD=admin SD=/tmp/obisweep sh tests/live/sweep.sh
#
# Exit 0 if every check passed.
set -u
O=./bin/obicfg
D="${SD:?set SD to a scratch directory}/live"
mkdir -p "$D"
pass=0; fail=0
ok()   { pass=$((pass+1)); printf 'PASS  %s\n' "$1"; }
bad()  { fail=$((fail+1)); printf 'FAIL  %s\n     %s\n' "$1" "$2"; }
want() { desc=$1; expect=$2; shift 2; o=$("$@" 2>&1); g=$?
         [ "$g" = "$expect" ] && ok "$desc (exit $g)" || bad "$desc" "want $expect got $g: $(echo "$o"|head -1)"; }
has()  { desc=$1; needle=$2; shift 2; o=$("$@" 2>&1)
         case "$o" in *"$needle"*) ok "$desc";; *) bad "$desc" "missing '$needle'";; esac; }

echo "--- 1. backup ---"
want "dump writes a backup"                0 $O dump "$D/before"
want "dump refuses to clobber the backup"  1 $O dump "$D/before"
want "dump --force overwrites deliberately" 0 $O dump "$D/before2" --no-raw
want "diff against a fresh backup is clean" 0 $O diff "$D/before"

echo "--- 2. reads ---"
want "status"            0 $O status --json
want "pages"             0 $O pages --json
want "pages --titles"    0 $O pages --titles
want "show"              0 $O show sp4 --json
want "show --changed"    0 $O show sp2 --changed --json
want "search"            0 $O search CallerIDName --json
want "search no match"   1 $O search zzzznope --json
want "get"               0 $O get sp4.CallerIDName
want "telemetry"         0 $O telemetry --json
want "telemetry no-calls" 0 $O telemetry --no-calls --json
want "telemetry redact"  0 $O telemetry --redact --calls 3 --json
has  "redaction hides the serial" '"serial": "<redacted>"' $O telemetry --redact --no-calls --json

echo "--- 3. writes on inert parameters ---"
want "set --dry-run sends nothing"   0 $O set sp4.CallerIDName=Scratch --dry-run --json
want "dry run really was a no-op"    0 $O diff "$D/before"
want "set a single value"            0 $O set sp4.CallerIDName=Scratch --json
has  "the value is now live" "Scratch"  $O get sp4.CallerIDName
want "device now differs from backup" 1 $O diff "$D/before"
want "re-setting the same value is a noop" 0 $O set sp4.CallerIDName=Scratch --json
want "set across two pages"          0 $O set sp4.CallerIDName=Two itsp.d.sip.ProxyServer=192.0.2.77 --json
has  "page one took"  "Two"          $O get sp4.CallerIDName
has  "page two took"  "192.0.2.77"   $O get itsp.d.sip.ProxyServer

echo "--- 4. values the old curl method could never send ---"
want "a value containing a space"  0 $O set 'sp4.CallerIDName=Two Words' --json
has  "the space survived" "Two Words" $O get sp4.CallerIDName
want "a value containing a hash"   0 $O set 'sp4.CallerIDName=Desk #2' --json
has  "the hash survived" "Desk #2"    $O get sp4.CallerIDName
want "query transport refuses a space" 1 $O --transport query set 'sp4.CallerIDName=a b' --dry-run
want "query transport carries braces"  0 $O --transport query set 'itsp.d.sip.ProxyServer={x}' --json
has  "braces stored verbatim" "{x}"    $O get itsp.d.sip.ProxyServer

echo "--- 5. validation and protection ---"
want "enum rejected"          1 $O set itsp.d.sip.ProxyServerTransport=SCTP --dry-run
want "range rejected"         1 $O set itsp.d.sip.ProxyServerPort=99999 --dry-run
long=$(python3 -c "print('x'*300)")
want "over-long string"       1 $O set "sp4.CallerIDName=$long" --dry-run
want "literal default"        1 $O set sp4.CallerIDName=default --dry-run
want "read-only parameter"    1 $O set sp4.RegistrationState=x --dry-run
want "guard: provisioned SP1" 4 $O set sp1.AuthUserName=x --dry-run
want "guard: auto-provisioning page" 4 $O set DM_S_.anything=1 --dry-run
want "guard: setup wizard"    4 $O set SetupWizard.anything=1 --dry-run
want "bad usage"              2 $O set notapair

echo "--- 6. profiles ---"
cat > "$D/scratch.toml" <<'TOML'
name = "Live scratch profile"
[require]
model = "OBi2"
[settings]
"sp4.CallerIDName" = "Profiled"
"itsp.d.sip.ProxyServer" = "192.0.2.88"
TOML
want "apply --dry-run"              0 $O apply "$D/scratch.toml" --dry-run
want "apply"                        0 $O --yes apply "$D/scratch.toml"
has  "profile applied" "Profiled"   $O get sp4.CallerIDName
has  "apply is idempotent" "already matches" $O --yes apply "$D/scratch.toml"
cat > "$D/wrongbox.toml" <<'TOML'
name = "Wrong model"
[require]
model = "OBi110"
[settings]
"sp4.CallerIDName" = "Nope"
TOML
want "require stops the wrong model" 1 $O apply "$D/wrongbox.toml" --dry-run

echo "--- 7. reset ---"
want "unset --dry-run"        0 $O unset sp4.CallerIDName --dry-run --json
has  "dry run did not reset" "Profiled" $O get sp4.CallerIDName
want "unset"                  0 $O --yes unset sp4.CallerIDName itsp.d.sip.ProxyServer

echo "--- 8. probe ---"
want "probe round-trips all three encodings" 0 $O --yes probe --scratch sp4.CallerIDName

echo "--- 9. restore proof ---"
want "device matches the opening backup" 0 $O diff "$D/before"

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
