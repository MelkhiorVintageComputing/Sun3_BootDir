# Netbooting a Sun-3

Everything needed to boot a Sun-3 (68020: 3/50, 3/60, 3/110, 3/150, 3/160,
3/260) over Ethernet from this Debian host, plus the scripts to rebuild it all
from an empty directory.

Almost nothing here runs with privilege. Exactly one script needs root, it is
about ten lines long, and it is run once.

## What the Sun-3 does, and who answers

```
    Sun-3                                    this host
    -----                                    ---------
 1. "who am I?"          --- RARP  ------->   rarpd          etc/ethers
 2. "send me C0A80079"   --- TFTP  ------->   atftpd         tftpboot/
 3. runs netboot, asks   --- PMAPPROC_CALLIT  rpcbind ---.
    "where is my root?"      to 255.255.255.255:111       `-> rpc.bootparamd
                                                              etc/bootparams
 4. mounts it and reads  --- NFSv3 ------->   unfsd          etc/exports
    the kernel                                              nfsroot/sun3/
 5. runs netbsd-RAMDISK
```

Step 3 is the one that fails silently: `netboot` does not look the boot
parameter server up by name, it broadcasts an indirect RPC call and waits.
`tools/bp-probe.py` reproduces that exchange so you can see it working without
touching the Sun-3.

## Quick start

```sh
$EDITOR config/sun3boot.conf      # list your machines in CLIENTS
scripts/setup.sh                  # download, build, configure  (no privilege)
scripts/dryrun.sh                 # rehearse the RPC stack      (no privilege)

sudo root/grant-privileges.sh     # once, after reading it

scripts/start.sh                  # bring the daemons up
scripts/selftest.sh               # check them from this host
scripts/status.sh -f              # then power on the Sun-3 and watch
```

At the Sun-3's PROM monitor:

```
>b le()
```

## Configuration

`config/sun3boot.conf` is the only file you edit. `scripts/03-configure.sh`
regenerates everything else from it and is safe to re-run:

| generated | used by | holds |
|---|---|---|
| `etc/ethers` | rarpd (via a `/etc/ethers` symlink) | MAC → IP, one line per client |
| `etc/hosts` | bootparamd (via a private mount namespace) | names, both directions |
| `etc/bootparams` | rpc.bootparamd | where each client's root is |
| `etc/exports` | unfsd | who may mount what |
| `tftpboot/C0A80079` | atftpd | → the netboot program |
| `nfsroot/sun3/netbsd` | unfsd | the kernel, hard-linked |

The TFTP filename is the client's IP in uppercase hex. 192.168.0.121 becomes
`C0A80079`. A sun3x asks for `C0A80079.SUN3X` instead; give it `sun3x` in the
table and pick a `3X` kernel.

## More than one client

`CLIENTS` in the config is a table, one line per machine:

```sh
CLIENTS='
sun3	08:00:20:11:22:33	192.168.0.121	sun3
sun3b	08:00:20:51:45:4D	192.168.0.122	sun3
'
```

An emulated Sun-3 is just another line. `sun3b` above is QEMU on a tap bridged
into `br0`; its PROM takes its address from RARP exactly like real hardware, so
the address is set here and nowhere else. The tap must be bridged rather than
on user-mode networking — slirp terminates the guest's traffic internally and
never passes RARP to the wire. See "Clients on more than one interface" below.

`03-configure.sh` writes an `etc/ethers`, `etc/hosts`, `etc/bootparams` and
`etc/exports` line, a `tftpboot/` link and an `nfsroot/<name>/` tree for every
entry. The kernels are hard-linked, so a second machine costs no disk. The
daemons need no per-client configuration at all — one `atftpd`, one
`bootparamd` and one `unfsd` serve them all. `03-configure.sh` refuses a table
with a duplicate name, MAC or address, since rarpd would answer whichever came
first and two clients sharing a name would share a root.

An `nfsroot/<name>/` belonging to a client you delete from the table is left
alone rather than removed; only the kernel names are managed.

The probes take `--client <name|IP>` to target one:

```sh
python3 tools/bp-probe.py --client sun3b
python3 tools/nfs-probe.py --client sun3b
python3 tools/tftp-bcast-probe.py --client sun3b
```

### Every client must be reachable on-link

RARP is answered over a raw socket, so it works for any address on the wire.
Everything after it is ordinary routed IP: TFTP, bootparams and NFS replies go
wherever the host's routing table sends them, not back down the interface the
request arrived on. A client the host would answer via a gateway therefore gets
its address from RARP and then goes silent, which reads as a TFTP fault and is
not one.

`03-configure.sh`, `status.sh` and `selftest.sh` all check this with
`ip route get` and name the interface each client would be answered on.

The fix is to renumber the client in `CLIENTS`, which needs no privilege and,
for a machine that gets its address from RARP, needs no change at the client
end either. The alternative — giving the host another address with
`ip addr add` — needs root, does not survive a reboot, and is deliberately not
part of `root/grant-privileges.sh`.

### Clients on more than one interface

`SERVER_IF` is a list, because real hardware on the LAN port and an emulator on
a bridge are on different segments:

```sh
SERVER_IF='eno1 br0'
```

Only rarpd cares — everything else binds `0.0.0.0` and is routed. rarpd binds
one interface per instance, so with more than one listed `start.sh` gives it
`-a` instead of an interface name. It then answers on whichever interface the
request arrived on, which is what we want, and `-b` still keeps it quiet for
machines that have no boot file.

Here `br0` bridges only the emulator's tap and carries `SERVER_IP` as a second
address added `noprefixroute`, with a host route for the guest alone:

```
192.168.0.0/24   dev eno1 proto kernel scope link src 192.168.0.31
192.168.0.122    dev br0  scope link src 192.168.0.31 linkdown
```

That is what keeps this safe: `eno1` still owns the subnet route and the
default route and is never reconfigured, so remote access to the host cannot be
lost. Both clients work at once.

rarpd enumerates interfaces once, at startup, and skips any that are not
`IFF_UP` — `start.sh` warns about that. Carrier is a different matter: a bridge
whose tap has no emulator attached shows `NO-CARRIER` and `operstate down`
while still being `UP`, rarpd enumerates it quite happily, and `selftest.sh`
reports it as `(no carrier yet)` rather than a failure.

## Scripts

| script | privilege | what it does |
|---|---|---|
| `scripts/setup.sh` | none | runs the numbered scripts below, in order |
| `scripts/00-fetch-packages.sh` | none | `apt-get download` + `dpkg-deb -x` into `pkg/` |
| `scripts/01-build-rpcbind.sh` | none | builds patched upstream rpcbind |
| `scripts/01-build-atftpd.sh` | none | builds patched upstream atftpd |
| `scripts/01-build-unfs3.sh` | none | builds unfs3 |
| `scripts/02-fetch-payload.sh` | none | fetches NetBSD/sun3 netboot + kernel, checksummed |
| `scripts/03-configure.sh` | none | generates `etc/`, `tftpboot/`, `nfsroot/` |
| `root/grant-privileges.sh` | **root, once** | three `setcap`s and one symlink |
| `scripts/start.sh` | none | starts the daemons (`m1`/`m2`/`m3`, or one by name) |
| `scripts/stop.sh` | none | stops them |
| `scripts/status.sh` | none | what is running, listening, registered; `-f` tails logs |
| `scripts/dryrun.sh` | none | rehearses the whole stack in a private namespace |
| `scripts/selftest.sh` | none | checks the real, running stack |

`start.sh m1` runs only RARP and TFTP, `m2` adds bootparams, `m3` adds NFS.
Bringing it up in stages makes a failure point at one protocol instead of five.

## What needs root, and why only that

| daemon | needs | why |
|---|---|---|
| `rarpd` | `cap_net_raw` | RARP replies come from an `AF_PACKET` socket |
| `atftpd` | `cap_net_bind_service` | TFTP is udp/69 |
| `rpcbind` | `cap_net_bind_service` | the portmapper is udp+tcp/111 |
| `rpc.bootparamd` | nothing | ephemeral port, registers with rpcbind |
| `unfsd` | nothing | 2049 is already unprivileged |

`root/grant-privileges.sh` grants those three capabilities and points
`/etc/ethers` at `etc/ethers`. The symlink is there because `rarpd` has no
option to relocate its MAC table; with it, changing the Sun-3's address never
needs root again.

Two things did **not** need root, and are worth knowing about because they look
like they should:

* **`/etc/hosts`.** `rpc.bootparamd` identifies a client by reverse-resolving
  its address, so it wants a hosts entry. Rather than edit the system file,
  `start.sh` gives that one daemon a private mount namespace with `etc/hosts`
  bind-mounted over `/etc/hosts`. Its network namespace is untouched, so it
  still registers and serves normally.
* **Installing packages.** `apt-get download` and `dpkg-deb -x` work as an
  ordinary user, and every library these packages need is already on a normal
  Debian 12 system. Nothing is installed system-wide.

## Why atftpd is built rather than installed

The Sun-3 does not learn the server's address from RARP, so it **broadcasts**
its TFTP read request to 255.255.255.255. atftpd takes the destination address
out of `IP_PKTINFO` (`ipi_addr`, not `ipi_spec_dst`) and binds the per-transfer
data socket to it — to the broadcast address. The first block goes out, the
client's ACK is addressed to the server's real address, and a socket bound to
255.255.255.255 never receives it. Both ends time out, having logged what looks
like a normal start:

```
Creating new socket: 255.255.255.255:58938
Serving C0A80079 to 192.168.0.121:1756
timeout block 0: retrying ...
client (192.168.0.121) not responding
```

atftpd has a `--listen-local` option whose surrounding comment describes exactly
this, but a 2011 change for Debian bug 613582 stopped it from rebinding, so it
now only sets `SO_BROADCAST` — which that same comment says does not help.
`src/atftpd-broadcast.patch` restores the rebind, under `--listen-local` only.
It is nine lines.

tftpd-hpa is not an escape hatch: 5.2 and 5.4 both use `ipi_addr` the same way
and neither special-cases a broadcast destination.

`tools/tftp-bcast-probe.py` reproduces the PROM's behaviour from this host, so
the fix is testable without the Sun-3. Against the stock Debian binary it stalls
after block 1; against the patched build it completes.

## Why rpcbind is built rather than installed

Debian's `rpcbind` cannot be used here, for two independent reasons:

* It refuses to start unless `geteuid()` is 0, and takes a lock under `/run`.
  A capability does not help with either — the check is on the uid, not on any
  privilege.
* Debian disables RPC indirect calls and hides them behind `-r`. The Sun-3
  finds `bootparamd` by sending `PMAPPROC_CALLIT` to the broadcast address, so
  indirect calls are not optional.

`scripts/01-build-rpcbind.sh` builds the same upstream 1.2.6 source Debian
uses, configured with `--enable-rmtcalls`, with `src/rpcbind-unprivileged.patch`
applied. That patch removes the uid check, makes the lock path a compile-time
setting, and stops the privilege-drop from being fatal when there are no
privileges to drop. It touches nothing else, and behaviour as root is unchanged.

`rpcbind` is started with `-i`, which lets our unprivileged daemons register
from ephemeral source ports. The cost is that any host on the LAN could
register an RPC service. Avoiding it would mean giving `unfsd` and
`rpc.bootparamd` capabilities of their own, which trades one loosened flag for
two more privileged binaries; on a private LAN `-i` is the better bargain.

### Alternative: use the system rpcbind

If you would rather not run a locally built portmapper, an administrator can
install Debian's instead. It runs as root permanently, which is the trade:

```sh
apt-get install rpcbind
echo 'OPTIONS="-i -r"' >/etc/default/rpcbind
systemctl enable --now rpcbind
```

Then drop `rpcbind` from `start.sh` and `01-build-rpcbind.sh` from `setup.sh`.
Everything else is unchanged.

## Verifying without the Sun-3

`scripts/dryrun.sh` starts the whole stack inside an unprivileged user,
mount, network and PID namespace, where ports 69 and 111 are free, and drives
it with the same probes. It proves TFTP, the portmap/bootparams exchange and
the NFSv3 mount before anyone is asked for root:

```
  PASS  portmapper (100000) registered
  PASS  bootparam (100026) registered
  PASS  mountd (100005) registered
  PASS  nfs (100003) registered
  PASS  C0A80079 served, 23316 bytes, matches the netboot image
  PASS  broadcast read request answered from a usable source address
  PASS  bootparamd answered
  PASS  unfsd served the kernel
```

`scripts/selftest.sh` runs the same checks against the real daemons once they
are up.

RARP is the one step neither can cover: generating a RARP request needs a raw
socket of its own. Watch `log/rarpd.log` (`scripts/status.sh -f`) while the
Sun-3 boots.

## Reading the logs

Each daemon logs to `log/<name>.log`, and `scripts/status.sh -f` tails them
together — useful next to a serial console.

`log/rpcbind.log` always contains

```
rpcbind: cannot bind local: Permission denied
```

That is `/run/rpcbind.sock`, which only root can create. It is not fatal:
rpcbind ignores the failure, and libtirpc falls back to loopback for service
registration. `status.sh` says so explicitly so it does not send you chasing it.

## Booting something other than NetBSD

Set `PAYLOAD=custom` in the config and point `CUSTOM_NETBOOT` and
`CUSTOM_KERNEL` at your own files. `02-fetch-payload.sh` then only checks they
exist; everything downstream is unchanged.

One caveat, and it is a real one. **unfs3 speaks NFSv3 only.** That is fine for
NetBSD, whose bootloader tries MOUNT v3 first and only falls back to v1
(`sys/lib/libsa/nfs.c`). A **SunOS 4.x** boot program speaks NFSv2 and nothing
else, so it cannot load a kernel from `unfsd`. This host's kernel is built with
`CONFIG_NFSD_V2` unset, so the in-kernel server cannot fill the gap either.
Going down the SunOS route means finding an NFSv2 server first; RARP, TFTP and
bootparams all work unchanged.

## A real NFS root, later

Milestones 1 to 3 deliberately end at `netbsd-RAMDISK`, a kernel that carries
its own root filesystem, so nothing has to solve the hard part yet.

A real NetBSD root needs device nodes in `/dev` and correct file ownership,
and `unfsd` running as an ordinary user can neither `mknod` nor `chown`. When
that becomes the goal it needs a decision that this setup has so far avoided:
run `unfsd` as root, or prepare the root filesystem some other way. Worth
deciding then, with the boot chain already proven.

## Layout

```
config/     the one file you edit
scripts/    everything unprivileged
root/       the one thing that is not
tools/      protocol probes (bp-probe.py, nfs-probe.py, tftp-bcast-probe.py)
            and sun3conf.py, which reads the client table for them
sbin/       the daemons; three carry capabilities
etc/        generated configuration
tftpboot/   what the PROM downloads
payload/    netboot and kernels as fetched
nfsroot/    what the client mounts
dist/ pkg/ src/   download cache, extracted .debs, local builds
run/ log/   pidfiles and logs
```
