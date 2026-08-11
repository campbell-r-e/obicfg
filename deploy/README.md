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

## Check it

```sh
systemctl status obicaller   # or: rc-service obicaller status
tail -f /var/log/obicaller.jsonl
```

No events at all usually means the device is not pointed at you, the level is
too low, or a firewall is dropping UDP on the listener.
