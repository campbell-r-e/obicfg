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
