# Running obicaller as a service

`contrib/obicaller.py` is a single standard-library file, so deploying it is a
copy plus one unit file. Both flavours are here: `obicaller.service` for
systemd, `obicaller.openrc` for OpenRC.

## The one thing to get right: the port

**An OBi sends syslog to exactly one address and port.** That constraint
drives everything else:

- If nothing else wants the stream, listen on whatever port you like and point
  the device straight at it.
- If something already collects it — another daemon, a log server, a
  monitoring agent — then pointing the device at that collector means nothing
  else can have the feed, and pointing it anywhere else silently starves the
  collector.

The way out is to listen on a *different* port and relay:

```sh
obicaller.py --port 5515 --forward 127.0.0.1:5514
```

Every datagram is passed on verbatim, so the existing consumer parses it
exactly as before. The relay is fire-and-forget: if the other side is down,
the live view keeps working.

## Install

```sh
install -D -m755 contrib/obicaller.py /opt/obicaller/obicaller.py

# systemd
install -D -m644 deploy/obicaller.service /etc/systemd/system/obicaller.service
systemctl daemon-reload && systemctl enable --now obicaller

# OpenRC
install -D -m755 deploy/obicaller.openrc /etc/init.d/obicaller
rc-update add obicaller default && rc-service obicaller start
```

Ports below 1024 need root. The defaults use 5515 so the daemon can run
unprivileged.

## Point the device at it

The only step that writes to the ATA, and it is reversible:

```sh
obicfg listen --setup      # works out your address and prints these for you

obicfg set admin.Server=<listener address> admin.Port=5515 admin.Level=7
obicfg unset admin.Server  # to undo
```

`Level` is syslog verbosity: 7 is the most detailed, lower is quieter, and 0
effectively silences the feed.

## Before you enable it

This puts call metadata — including caller numbers — on your local network in
the clear, and into a log file on the listener. That is inherent to the
device's syslog feature rather than to this script, but it is a real
consequence and worth a moment's thought on a shared network. `--calls-only`
narrows what is logged; omitting `--log` keeps it out of a file entirely.

## Speaking the caller ID aloud

```sh
obicaller.py --port 5515 --say --amplitude 200 --speed 140 --say-between 8-22
```

Caller ID arrives on a line of its own — `<7> [SLIC] CID to deliver: '+1555…'`
— which none of the generic call wording matches, so this is the pattern that
makes an announcement possible at all. A withheld number comes through as two
apostrophes and is read as "private caller". Digits are spoken one at a time,
because espeak given a bare number says "five million five hundred fifty-one
thousand…". `--say-between` suppresses announcements overnight; the default
8–22 is the original's. All of that is obicaller's work, not mine.

Speech is rendered to a WAV and piped to `aplay` rather than letting espeak
open the sound device, so the player decides where the audio goes. On a
minimal install you may need the ALSA tools and an unmuted mixer:

```sh
apk add alsa-utils          # or: dnf install alsa-utils
amixer sset Master 85% unmute
amixer sset Speaker 85% unmute
alsactl store               # keep it across reboots
```

## Keeping a dead cloud's traffic off the internet

The device keeps reaching for the retired OBiTALK service: `root.pnn.obihai.com`
(which Poly left resolving to `127.0.0.1`), plus `prov.obitalk.com` and
`devpfs.obitalk.com`, which still resolve to live CloudFront addresses. None of
it can succeed. A local resolver answers the lot without going out:

```
# dnsmasq
address=/obihai.com/127.0.0.1
address=/obihai.com/::1
address=/obitalk.com/127.0.0.1
address=/obitalk.com/::1
```

Both address families matter: an `address=` line covers A only, and AAAA
falls through to upstream — which is exactly how `devpfs.obitalk.com` kept
leaking after the first attempt.

Then point the ATA at that resolver, keeping a second one as fallback, and
reboot (the WAN page needs it):

```sh
obicfg set wan.DNSServer1=<resolver> wan.DNSServer2=<router>
obicfg reboot --wait
```

> **This does not touch Google Voice.** A GV-provisioned OBi reaches
> `obihai.sip.google.com` and `obihai.telephony.goog` — under `google.com` and
> `.goog`, not `obihai.com`. Check yours first with
> `obicfg get itsp.a.sip.ProxyServer itsp.a.sip.OutboundProxy`, and confirm
> the service still reads "Connected" after the reboot.

Worth knowing before you bother: measured on a real device, the ATA issues
only **8 DNS queries in the ~20 seconds after boot** and then caches. The
9-per-second syslog line is it *retrying the connection*, not re-resolving. So
this stops a small trickle of pointless outbound DNS and connection attempts,
not a flood.

## Storing events in PostgreSQL

`contrib/obi-syslog-pg.py` reads the JSON lines `obicaller.py --json` writes
and loads them with `COPY`. It shells out to `psql` rather than importing a
driver, so **no Python database package is needed** — which matters on a box
where you would rather not install one.

```sh
install -D -m755 contrib/obicaller.py     /opt/obicaller/obicaller.py
install -D -m755 contrib/obi-syslog-pg.py /opt/obicaller/obi-syslog-pg.py
install -D -m644 deploy/obi-syslog-pg.service /etc/systemd/system/

# credentials in libpq's own variables, mode 600, never on a command line
printf 'PGHOST=127.0.0.1\nPGDATABASE=obi\nPGUSER=obi\nPGPASSWORD=...\n' \
  | install -m600 /dev/stdin /etc/obi-syslog-pg.env

set -a; . /etc/obi-syslog-pg.env; set +a
python3 /opt/obicaller/obi-syslog-pg.py --create     # table and indexes
systemctl daemon-reload && systemctl enable --now obi-syslog-pg
```

If the database is on a different host from the listener, have the listener
`--forward` to it and run this pipeline there. On PostgreSQL's default Fedora
configuration a TCP connection uses ident auth, so a password login needs a
`pg_hba.conf` rule — scope it to the one database and user over loopback, and
put it **above** the generic `host all all` line, since pg_hba matches
top-down and first match wins.

> **`source` is whoever sent the datagram, not necessarily the ATA.** Behind a
> relay it is the relay's address, because that is what the socket reports.
> With one device that is harmless; with several, either point them at
> separate ports or key on something in the message.

Some useful queries:

```sql
-- recent calls
SELECT received_at, sp, call_event, number FROM syslog_event
 WHERE call_event IS NOT NULL ORDER BY received_at DESC LIMIT 20;

-- registration flapping, per service, by hour
SELECT date_trunc('hour', received_at) AS hour, sp, count(*)
  FROM syslog_event WHERE call_event IN ('registered','unregistered')
 GROUP BY 1, 2 ORDER BY 1 DESC;
```

## Check it

```sh
systemctl status obicaller   # or: rc-service obicaller status
tail -f /var/log/obicaller.jsonl
```

No events at all usually means the device is not pointed at you, the level is
too low, or a firewall is dropping UDP on the listener.
