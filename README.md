# Netbooting a Sun-3

Everything needed to boot a Sun-3 (68020: 3/50, 3/60, 3/110, 3/150, 3/160,
3/260) over Ethernet from this Debian host, plus the scripts to rebuild it all
from an empty directory.

Also boots a Sun-2, which needs an entirely different protocol; see "A Sun-2".

Almost nothing here runs with privilege. One script needs root and is run once;
a second, needed only to boot SunOS, adds a single address.

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
| `etc/bootparams` | rpc.bootparamd | each client's root, and its gateway |
| `etc/exports` | unfsd | who may mount what |
| `tftpboot/C0A80079` | atftpd, and rarpd's `-b` check | → the netboot program |
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
sun2_f_m	08:00:20:01:06:e0	192.168.0.123	sun2
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
| `scripts/01-build-ndbootd.sh` | none | builds ndbootd with a Linux AF_PACKET backend |
| `scripts/02-fetch-payload.sh` | none | fetches NetBSD/sun3 netboot + kernel, checksummed |
| `scripts/03-configure.sh` | none | generates `etc/`, `tftpboot/`, `nfsroot/` |
| `root/grant-privileges.sh` | **root, once** | four `setcap`s and one symlink |
| `root/allow-oldstyle-broadcast.sh` | **root** | one address, for a SunOS client only |
| `scripts/start.sh` | none | starts the daemons (`m1`/`m2`/`m3`, or one by name) |
| `scripts/stop.sh` | none | stops them |
| `scripts/status.sh` | none | what is running, listening, registered; `-f` tails logs |
| `scripts/dryrun.sh` | none | rehearses the whole stack in a private namespace |
| `scripts/selftest.sh` | none | checks the real, running stack |

`start.sh m1` runs only RARP and TFTP, `m2` adds bootparams, `m3` adds NFS.
Bringing it up in stages makes a failure point at one protocol instead of five.

## A Sun-2

A Sun-2 shares almost nothing with the Sun-3 boot chain. From `ndbootd(8)`:

> The Sun 2 PROMs can only use ND to boot over the network. (Later, the Sun 3
> PROMs would use RARP and TFTP to boot over the network.) [...] **Sun 2 PROMs
> don't do RARP**, but they do learn their IP address from the first ND
> response they receive from the server.

```
              Sun-3                     Sun-2
  address     RARP        rarpd         out of the first ND reply
  bootstrap   TFTP        atftpd        ND (IP protocol 77)   ndbootd
  root        bootparams + NFS          bootparams + NFS      (same daemons)
```

`rarpd` will never see a packet from a Sun-2. Its requests look like this, and
`tcpdump` cannot name the protocol because Debian's `/etc/protocols` has no
entry for 77 — IANA assigns it to `SUN-ND`:

```
08:00:20:01:06:e0 > ff:ff:ff:ff:ff:ff, ethertype IPv4 (0x0800),
    (ttl 4, id 0, proto unknown (77), length 48)
```

### ndbootd, and the AF_PACKET backend

`ndbootd` is NetBSD's ND server and is not packaged for Debian, so
`scripts/01-build-ndbootd.sh` builds it from the NetBSD tree, pinned to a
commit and checksum-verified. It needs one thing Linux does not provide:
its raw interface is `/dev/bpf`.

Upstream anticipates this. `ndbootd.h` declares the whole raw interface as
three functions and `ndbootd.c` `#include`s one backend at the bottom, so
`src/ndbootd-packet.c` supplies them with `AF_PACKET`:

| | BPF | AF_PACKET |
|---|---|---|
| open | `open(/dev/bpf)`, `BIOCSETIF`, `BIOCSETF` | `socket(AF_PACKET, SOCK_RAW)`, `bind`, `SO_ATTACH_FILTER` |
| filter | `struct bpf_insn` | `struct sock_filter` — identical encoding, reused verbatim |
| read | one `read()` returns many frames behind `bpf_hdr`s | one `recvfrom()` per frame |
| ignoring our own | compare source address | `PACKET_IGNORE_OUTGOING`, or `PACKET_OUTGOING` |
| write | `write()` | `sendto()` with a `sockaddr_ll` |

`src/ndbootd-linux.patch` covers the rest: hardware addresses come from an
`AF_PACKET` `getifaddrs` entry rather than `AF_LINK`, `struct sockaddr` has no
`sa_len`, `strlcpy` predates glibc 2.38, `reallocarr(3)` is NetBSD's alone, and
`<time.h>` and `<netinet/ether.h>` are not pulled in transitively. Every change
is conditional, so building on NetBSD is unaffected.

Link-layer access is not an optimisation: the client has no IP address until it
reads one out of our first reply, so there is no address to send to. That is
why `ndbootd` needs `CAP_NET_RAW` — the same capability `rarpd` already has, so
it is one more line in `root/grant-privileges.sh` rather than a new kind of
privilege.

### What ndbootd serves

ND exports what the client believes is a raw disk, `/dev/ndp0`:

```
block 0       a Sun disklabel, ignored by the PROM
blocks 1-15   the first stage    payload/sun2-bootyy   (start.sh passes it)
block 16 on   the second stage   tftpboot/C0A8007B.SUN2 (ndbootd -s finds it)
```

The second stage is found by the same hex-plus-suffix name a Sun-3 TFTPs by, so
`03-configure.sh` generates it exactly as it does for the others, and
`etc/ethers` is shared with `rarpd`. Both programs must be raw binaries with
executable headers stripped, which is how NetBSD ships them.

`start.sh` runs `ndbootd` only when the table has a `sun2` in it, and treats
every problem with it as a warning rather than an error, so a missing capability
or payload cannot stop the Sun-3 stack from coming up.

### Testing it without a Sun-2

`tools/nd-probe.py` sends an ND read request exactly as the PROM does — zero
source and destination addresses, broadcast — and reports the reply.
`scripts/dryrun.sh` builds a veth pair inside its namespace and runs the real
`ndbootd` on it, which is what actually exercises the AF_PACKET backend, and
needs no privilege at all:

```
  PASS  ND read of block 1 answered
          <- READ|WAIT|DONE from 192.168.0.31 to 192.168.0.123
             512 bytes of data, first 16: 46fc270041fafffa43f900240000b3c8
  PASS  ND read of block 16 answered
```

`46fc 2700` is `move #$2700,sr` — the first instruction of a 68000 boot
program, so that really is `bootyy` coming back.

### The kernel, and the name it is asked for

The sun2 kernel is a separate download from the sun3 one -- different
architecture, different MD5 file -- controlled by `NETBSD_KERNEL_SUN2`. It is
fetched only when the table has a `sun2` in it:

```
payload/netbsd-RAMDISK       ELF 32-bit MSB, m68k, 68020   sun3
payload/sun2-netbsd-RAMDISK  ELF 32-bit MSB, m68k, 68000   sun2
```

The name matters as much as the architecture. A Sun-2 PROM passes **`vmunix`**
to netboot, not `netbsd`:

```
Boot: ie(0,0,0)vmunix
>> NetBSD/sun2 netboot [1.13 ...]
open vmunix: No such file or directory
```

So `03-configure.sh` hard-links the kernel as `netbsd`, `vmunix` and
`netbsd-rd`, which is what NetBSD 10.1 `sun2/INSTALL.txt` asks for. `selftest.sh`
reads back the name each client will actually request -- `vmunix` for a sun2,
`netbsd` for a sun3 -- rather than assuming one name for everything.

## SunOS on the Sun-2

`sun2_f_m` can boot SunOS 4.0.3 instead of NetBSD. Which one it gets is the
fifth column of its `CLIENTS` line:

```sh
sun2_f_m	08:00:20:01:06:e0	192.168.0.123	sun2	sunos
```

Change that word to `netbsd`, re-run `scripts/03-configure.sh` and restart, and
it boots NetBSD again. The two are alternatives per machine, not per server:
the Sun-3s keep booting NetBSD either way.

The boot programs come from `../Sun-2_DiskImage/netboot/`, set by
`SUNOS_NETBOOT_DIR`. That directory is read only as far as this is concerned --
`02-fetch-payload.sh` verifies it against its own `SHA256SUMS` and copies out of
it, never into it.

### It is a different chain, not a different kernel

```
             NetBSD/sun2                     SunOS 4.0.3
  ND         bootyy, then netboot            sun2.bb only
  RARP       not used                        sun2.bb asks, rarpd answers
  TFTP       not used                        C0A8007B.SUN2 = boot.sun2
  bootparams whoami + getfile root           same
  NFS        version 3, unfs3                version 2, tools/nfs2d.py
  kernel     netbsd / vmunix                 vmunix
```

Only the middle two lines are shared. NetBSD carries ND all the way to its
second stage; SunOS drops ND after 15 blocks and finishes over RARP and TFTP,
which is why `rarpd` matters for a Sun-2 running SunOS and not for one running
NetBSD. `start.sh` picks `ndbootd`'s first stage from the payload word, and
says which one it chose.

### tools/nfs2d.py, a read-only NFSv2 server

SunOS 4.0.3 is from 1989. NFSv3 is from 1995. `boot.sun2` and the SunOS kernel
speak NFS version 2, which unfs3 does not serve, and this host's kernel has no
`CONFIG_NFSD_V2` (and would want root anyway). So `tools/nfs2d.py` serves NFS
version 2 and MOUNT version 1, in about 490 lines of Python, as an ordinary
user.

It runs **alongside** unfs3 rather than instead of it. RPC programs register per
version, so the two do not collide:

```
100003  3  udp  2049   nfs      unfs3     NetBSD clients
100005  3  udp  2049   mountd   unfs3
100003  2  udp  2050   nfs      nfs2d     SunOS clients
100005  1  udp  2050   mountd   nfs2d
```

Both Sun-3s and the Sun-2 can boot at the same time, each over the version it
understands. `nfs2d` registers itself through the portmapper, so its port is
not something a client has to be told.

It is deliberately **read-only**: everything that would modify the export
returns `NFSERR_ROFS`. That is enough to load a kernel, which is what
netbooting is. It is not enough for SunOS to then come up multiuser -- see
below.

`selftest.sh` reads each client's kernel back over the version that client will
actually use, `--nfs-version 2` for a SunOS client and 3 for the others.

### The all-zeros broadcast

SunOS's `/boot` gets its address from RARP, which carries no netmask, so when
it broadcasts its bootparams request it uses the 4.2BSD form -- the network
address with the host part zeroed, not `255.255.255.255`:

```
192.168.0.123.1023 > 192.168.0.0.111: UDP, length 100
```

Modern Linux installs a broadcast route for `192.168.0.255` and none for
`192.168.0.0`, so that datagram is dropped in the input path. Nothing logs it,
because no daemon ever sees it -- `rpcbind -d` shows the client's RARP, ND and
TFTP traffic and then simply nothing. On the console it reads as

```
Boot: bad dialog with bootparam server (error 0x4)
```

and `0x4` is the fifth entry of the RPC error table inside `boot.sun2` itself:
**"RPC: Unable to receive"**. It sent and heard nothing back, which is a
different thing from a timeout and worth distinguishing.

`root/allow-oldstyle-broadcast.sh` adds the address so the kernel accepts that
destination; `rpcbind` is already on `0.0.0.0:111`. It is the second and last
thing here that needs root, it is needed only for a SunOS client, and **it does
not survive a reboot** -- so `selftest.sh` and `status.sh` both check for it
whenever a `sunos` client is configured:

```
PASS  sun2_f_m: this host accepts the old-style broadcast 192.168.0.0
```

Worth knowing why this is invisible without a capture: ND and RARP reach their
daemons through `AF_PACKET`, which bypasses the IP input path entirely, and
TFTP is unicast to a real local address. The bootparams call is the only step
that needs Linux to accept a *broadcast* destination, which is exactly why it
is the only one that fails.

### How far this gets

To the kernel, and no further. The chain is proven up to and including
`boot.sun2` reading `vmunix` out of the NFS root. What happens next is that the
kernel does RARP and bootparams **again for itself**, mounts root, and tries to
run `/usr/etc/init` out of it.

That needs a populated, writable SunOS root filesystem, which is not in
`netboot/` and is not something this directory has. Expect the kernel to load,
start, and then fail to find a userland. Getting past that is the same problem
as "A real NFS root, later" below, with SunOS's ownership and device nodes on
top.

## What needs root, and why only that

| daemon | needs | why |
|---|---|---|
| `rarpd` | `cap_net_raw` | RARP replies come from an `AF_PACKET` socket |
| `ndbootd` | `cap_net_raw` | a Sun-2 has no address to reply to, so ND is link-layer |
| `atftpd` | `cap_net_bind_service` | TFTP is udp/69 |
| `rpcbind` | `cap_net_bind_service` | the portmapper is udp+tcp/111 |
| `rpc.bootparamd` | nothing | ephemeral port, registers with rpcbind |
| `unfsd` | nothing | 2049 is already unprivileged |
| `nfs2d` | nothing | an ordinary UDP port, found through the portmapper |

`root/grant-privileges.sh` grants those capabilities and points
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

**unfs3 speaks NFSv3 only**, which is fine for NetBSD -- its bootloader tries
MOUNT v3 first and only falls back to v1 (`sys/lib/libsa/nfs.c`). A **SunOS
4.x** boot program speaks NFSv2 and nothing else, and this host's kernel is
built with `CONFIG_NFSD_V2` unset, so neither unfs3 nor the in-kernel server
can serve it. That gap is filled by `tools/nfs2d.py`; see "SunOS on the Sun-2".

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
tools/      protocol probes (bp-probe.py, nfs-probe.py, tftp-bcast-probe.py,
            nd-probe.py) and nfs2d.py, a read-only NFSv2 server
            and sun3conf.py, which reads the client table for them
sbin/       the daemons; four carry capabilities
etc/        generated configuration
tftpboot/   what the PROM downloads
payload/    netboot and kernels as fetched
nfsroot/    what the client mounts
dist/ pkg/ src/   download cache, extracted .debs, local builds
run/ log/   pidfiles and logs
```
