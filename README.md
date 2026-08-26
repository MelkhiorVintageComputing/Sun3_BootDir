# Netbooting a Sun-3

Everything needed to boot a Sun-3 (68020: 3/50, 3/60, 3/110, 3/150, 3/160,
3/260) over Ethernet from this Debian host, plus the scripts to rebuild it all
from an empty directory.

Also boots a Sun-2, which needs an entirely different protocol; see "A Sun-2" --
two of them at once if you like, each running something different: SunOS 4.0.3
on one and NetBSD 2.0 with a real NFS root on the other.

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
 4. mounts it and reads  --- NFS   ------->   unfsd          etc/exports
    the kernel                                              nfsroot/sun3/
 5. runs netbsd-RAMDISK
```

Step 4 is version 3 unless a `netbsd2` client is configured, in which case
`nfs2d` answers it over version 2 and `unfsd` does not run at all; `netboot`
falls back on its own. See "Which server answers MOUNT".

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
| `ndboot/C0A8007C.SUN2` | ndbootd | → that Sun-2's ND first stage |
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
sun2_f_m	08:00:20:01:06:e0	192.168.0.123	sun2	sunos
sun2b	08:00:20:01:06:e1	192.168.0.124	sun2	netbsd2
'
```

The fifth column is what the machine boots: `netbsd` (the default, NetBSD 10.1
with a RAMDISK kernel), `sunos` (SunOS 4.0.3), or `netbsd2` (NetBSD 2.0 with a
real root filesystem). The last two are sun2-only.

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
| `scripts/02-fetch-payload.sh` | none | fetches netboot, kernels and sets for every payload in the table, checksummed |
| `scripts/03-configure.sh` | none | generates `etc/`, `tftpboot/`, `nfsroot/` |
| `root/grant-privileges.sh` | **root, once** | four `setcap`s and one symlink |
| `root/allow-oldstyle-broadcast.sh` | **root** | one address, for a SunOS client only |
| `root/make-sunos-root.sh` | **root, once** | unpacks a SunOS root (needs `mknod`) |
| `scripts/04-make-netbsd2-root.sh` | none | unpacks a NetBSD 2.0 root, `/dev` included |
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
blocks 1-15   the first stage    ndboot/C0A8007C.SUN2
block 16 on   the second stage   tftpboot/C0A8007C.SUN2
```

Both are found by the same hex-plus-suffix name a Sun-3 TFTPs by, so
`03-configure.sh` generates them exactly as it does the others, and
`etc/ethers` is shared with `rarpd`. Both must be raw binaries with executable
headers stripped, which is how NetBSD ships them.

Upstream `ndbootd` finds only the *second* stage that way; the first is one
file for all clients, given on the command line. Two Sun-2s can need entirely
different ones — NetBSD's `bootyy` reads block 16 onwards and stays on ND,
SunOS's `sun2.bb` stops using ND after block 15 and finishes over RARP and
TFTP — so `src/ndbootd-boot1-dir.patch` lets the trailing argument be a
directory and looks the first stage up in it by the same per-client name. The
open was already per-client; only the name was not. A plain filename still
behaves exactly as before.

That is why there are two directories rather than one: `tftpboot/C0A8007C.SUN2`
is already taken by the second stage, and `atftpd` has no business serving
first stages to anybody.

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
  PASS  08:00:20:01:06:e0: ND read of block 1 answered
  PASS  08:00:20:01:06:e0: block 1 is sunos-sun2.bb
  PASS  08:00:20:01:06:e0: ND read of block 16 answered
  PASS  08:00:20:01:06:e1: ND read of block 1 answered
  PASS  08:00:20:01:06:e1: block 1 is netbsd2-bootyy
  PASS  08:00:20:01:06:e1: ND read of block 16 answered
```

It asks as each Sun-2 in turn and compares the first sixteen bytes that come
back with the first sixteen of that client's own `ndboot/` entry, which is the
only way to see that they really did get different programs. `46fc 2700` is
`move #$2700,sr`, the first instruction of a 68000 boot program, so what comes
back is a boot program and not a disk label.

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
  NFS        version 2, tools/nfs2d.py       version 2, tools/nfs2d.py
  kernel     netbsd / vmunix                 vmunix
```

Only the middle two lines are shared. NetBSD carries ND all the way to its
second stage; SunOS drops ND after 15 blocks and finishes over RARP and TFTP,
which is why `rarpd` matters for a Sun-2 running SunOS and not for one running
NetBSD. `03-configure.sh` puts each machine's first stage in `ndboot/` under
its own name, so the two can be powered on at the same time.

Both end up on NFS version 2: every NetBSD bootstrap is version 2 only
(`sys/lib/libsa/nfs.c` in 2.0 knows nothing else, and 10.1 tries version 3 and
falls back), and so is every SunOS 4.x one. Version 3 only comes into it when
a NetBSD *kernel* mounts its root; see "Which server answers MOUNT".

### tools/nfs2d.py, an unprivileged NFSv2 server

SunOS 4.0.3 is from 1989. NFSv3 is from 1995. `boot.sun2` and the SunOS kernel
speak NFS version 2, which unfs3 does not serve, and this host's kernel has no
`CONFIG_NFSD_V2` (and would want root anyway). So `tools/nfs2d.py` serves NFS
version 2 and the MOUNT protocol, in about 950 lines of Python, as an ordinary
user.

It runs **alongside** unfs3 rather than instead of it -- RPC programs register
per version, so the two do not collide -- until a client turns up whose kernel
would pick the wrong one, at which point unfs3 stands down and nfs2d answers
for everybody ("Which server answers MOUNT"). With only `netbsd` and `sunos`
clients in the table it is the arrangement below:

```
100003  3  udp  2050   nfs      unfs3     NetBSD clients
100005  3  udp  2050   mountd   unfs3
100003  2  udp  2049   nfs      nfs2d     SunOS clients
100005  1  udp  2049   mountd   nfs2d
100005  2  udp  2049   mountd   nfs2d
```

MOUNT version 2 is version 1 plus a `PATHCONF` procedure (`MOUNTVERS_POSIX`),
so answering both costs one line and nothing else. It is there because a
NetBSD kernel walks the versions downwards and needs to find one; see "Which
server answers MOUNT".

Which of the two servers sits on 2049 is not a free choice; see the next
section.

Both Sun-3s and the Sun-2 can boot at the same time, each over the version it
understands. `nfs2d` registers itself through the portmapper, so its port is
not something a client has to be told.

### A handle names a file, not a name

A real NFS server derives a file handle from the inode, so a rename does not
disturb it: a client goes on writing through the handle it opened with and the
bytes land in the renamed file. These handles are derived from the path, which
is what makes them survive a restart — and what makes a rename break them.

That is not a corner case. It is what a compiler does:

```
WRITE l.outa00023 16384+512 -> OK
RENAME sun2_f_m/tmp/l.outa00023 -> sun2_f_m/tmp/dhry
READ  -> NOENT
```

`ld` writes its output under a temporary name, renames it over the target, and
carries on through the descriptor it already had. So `Export` keeps an alias
for the handle minted from the old name, pointing at the new one, and a chain
of renames drags every handle in it along. The aliases are re-applied after a
rescan, since a walk of the tree can never rediscover a handle whose name no
longer exists — and a rescan is triggered by a handle miss, which is exactly
when one is being looked up.

### Silence is the one answer a client cannot use

The same trace ended in a loop, and the reason is worth stating on its own.
`nfs2d` answered a `WRITE` to the renamed-away file with **nothing at all** —
an `lstat` outside a `try`, an exception caught by the top-level handler, and
`reply = None`. The client retransmitted at 4.6s, 9.2s, 18.4s, 36.9s and then
every 63 seconds, for ever.

An error is a fine answer; no answer is not one, because a client has no way
to stop asking for it. So the dispatcher now wraps every NFSv2 call and
answers `NFSERR_IO` on anything unexpected — every NFSv2 reply begins with a
status, and a non-zero one means nothing follows, so that is well-formed for
any procedure — and names the client, the procedure and the exception in the
log instead of printing a bare `FileNotFoundError`. A handle whose file is
genuinely gone gets `NFSERR_STALE`, which is what it means: `NOENT` is about a
name the client asked for, and here it did not ask for one.

`nfs-probe.py --check-rename` covers both. It creates a file, writes through
the handle, renames it, writes through the *pre-rename* handle, reads the
result back whole, then removes the file and requires `STALE` rather than
silence.

### 2049 has to be the NFSv2 server

SunOS asks the portmapper for **mountd** and then sends **NFS straight to
2049** without looking it up -- 2049 is the well-known port, and a 1989 client
simply assumes it. With unfs3 there, the mount succeeds and the very next call
fails:

```
NFS getattr failed for server x11spl: RPC: Program/version mismatch
Boot: unable to mount root (error 0x10)
```

unfs3 is answering correctly; it serves version 3 and was asked for version 2.
So `nfs2d` takes 2049 and unfs3 moves to `UNFSD_PORT`, which costs NetBSD
nothing because it looks up both mountd and nfs by program number:

```
100003  2  udp  2049   nfs      nfs2d    SunOS -- assumes this port
100005  1  udp  2049   mountd   nfs2d
100005  2  udp  2049   mountd   nfs2d
100003  3  udp  2050   nfs      unfs3    NetBSD -- asks the portmapper
100005  3  udp  2050   mountd   unfs3
```

A portmapper-based probe cannot catch this, because the lookup answers
correctly while the real boot fails. `selftest.sh` therefore checks a SunOS
client twice: once through the portmapper, and once with `--nfs-port 2049` to
do it the way the machine does.

### Swap, and why the export is not entirely read-only

A diskless SunOS kernel asks bootparams for **`swap`** immediately after
mounting root, and retries forever if nobody answers. With only `root` in the
entry it mounts, sizes the filesystem, and stops there:

```
nfs2d       MNT /…/nfsroot/sun2_f_m -> ok
nfs2d       GETATTR sun2_f_m -> OK
nfs2d       STATFS  sun2_f_m -> OK
bootparamd  getfile got question for "sun2_f_m" and file "swap"
bootparamd  getfile failed for sun2_f_m           (× 39, and counting)
```

Neither `GETATTR` nor `STATFS` says anything about a file *inside* the root, so
no amount of reading them reveals a missing `init` — a client stuck here never
reaches a `LOOKUP` at all. The absence of failures was not evidence that
nothing was wrong.

`03-configure.sh` gives every `sunos` client a `swap=` entry and a sparse
`nfsroot/<name>/swap` of `SUNOS_SWAP_MB`, created only when absent so that
re-running it cannot wipe the swap of a machine that is up.

Swapping means writing, so `nfs2d` is no longer entirely read-only — but the
exception is one named file. `start.sh` passes `--writable` for each client's
swap and nothing else, so the kernels in the same export still refuse:

```
192.168.0.123 WRITE swap 4096+512   -> OK
192.168.0.123 WRITE sun2_f_m/vmunix -> ROFS
```

`CREATE`, `REMOVE`, `MKDIR` and the rest are refused unconditionally: nothing
can add or remove a name. `selftest.sh` checks both halves — that swap survives
a write and read-back, and that the kernel beside it is refused.

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

### A real SunOS root

`root-4.0.3.tar.gz` in the SunOS netboot directory is a complete 4.0.3 root --
2780 members, 113 device nodes, 147 symlinks, 50 setuid binaries, `/usr`
included so the client mounts nothing else. `root/make-sunos-root.sh` unpacks
it for a client:

```sh
sudo root/make-sunos-root.sh            # or: ... sun2_f_m
```

It reads the client's name and address out of `config/sun3boot.conf` rather
than taking them again, so they cannot drift from what the daemons serve, and
hands off to that directory's own `mkroot`, which patches `etc/rc.boot`,
`etc/hosts` and `etc/fstab` to match.

Root is needed for one thing: `mknod`. Unpacked as an ordinary user the tree
comes out with an empty `/dev` and the client cannot open its console, which is
why `mkroot` refuses to run as anyone else.

### Root-owned tree, unprivileged server

That leaves a problem worth naming, because it is the one this whole directory
has been deferring since the beginning. A SunOS root is owned by root
throughout. `nfs2d` runs as an ordinary user, so it could read some of that
tree and write none of it — and the obvious fix, running the NFS server as
root, is exactly what everything here has been built to avoid.

So the script does a second thing: it gives the tree to the user who runs the
daemons, and `start.sh` passes `--squash-to-root`, which reports every file to
the client as owned by root. The client sees the ownership SunOS expects; the
server can do the I/O. `chown` clears setuid and setgid bits, so the 50 files
that carry them are recorded first and restored afterwards.

Root is therefore needed **once, to unpack**, and never at runtime.

`start.sh` notices a real root by looking for `etc/rc.boot` and switches from
`--writable <swap file>` to `--writable-tree <the whole root>`. A client that
only swaps stays exactly as narrow as it was; `CREATE`, `REMOVE`, `MKDIR` and
the rest are still refused outside a writable tree.

### "ie0: giant packet" — a client fault, not a server one

Recorded here because the evidence is on this side, and because it is easy to
mistake for a server problem. The console says

```
ie0: giant packet
NFS server x11spl not responding still trying
```

while the server's log shows it answering perfectly, over and over, at the same
offset:

```
192.168.0.123 READ init 0+8192 -> 8192       (× 18, all at offset 0)
```

The sizes locate the fault precisely:

| what | on the wire | result |
|---|---|---|
| a 1024-byte NFS reply | one 1166-byte frame | works — 748 of them in a row |
| an 8192-byte NFS reply | 8320 bytes of UDP → six fragments, five full-size | every one dropped |

The interface cannot receive a 1514-byte frame. Nothing in the protocol is
wrong and nothing here is misconfigured: full-size frames are ordinary
Ethernet, and every other client on the same wire takes them.

**This directory does not work around it.** A per-host route MTU would hide it
convincingly, which is exactly the argument against: the machine would then
appear to work while still dropping any full-size frame from anything else on
the network. It belongs in whatever fixes the hardware.

Two things that are *not* the lever, so nobody spends the afternoon on them:

* **The client cannot be asked for less *at boot*.** `STATFS` carries `tsize`
  — "the number of bytes the server would like to have in the data part of
  READ and WRITE requests" (RFC 1094) — and `NFS2D_TSIZE` sets it. A SunOS
  kernel mounting its **root** ignores it and has no mount options to pass at
  boot; it asked for 8192 again after a reboot. (`boot.sun2`'s 1024-byte reads
  are its own choice, not ours, which is why the kernel loads and then
  userland does not.)

  Once that userland is up it is a different matter: the same client honours
  `tsize` for writes, and every WRITE it sends is 1024 bytes or the short tail
  of one. That is 8× the RPCs an 8192-byte `wsize` would need, and it is also
  why a write never has to be fragmented — so the knob does earn its keep,
  just not at the point in the boot where it was first reached for.
* **The server cannot send less.** A short NFSv2 READ is how end of file is
  signalled, so trimming a reply would silently truncate the file rather than
  slow it down. `MAXDATA` stays at 8192 for that reason.

### How far this gets

Into userland. The kernel does RARP and bootparams **again for itself**, mounts
the root, and execs `/sbin/init` out of it. `log/nfs2d.log` shows exactly that,
and it is worth knowing how to read, because the client's console says far less:

```
LOOKUP sbin in sun2_f_m -> OK     the kernel resolving /sbin/init
LOOKUP init in sun2_f_m/sbin -> OK
READ init 0+8192 -> 8192          six of these: 6 x 8192 = 49152, init's
...                               exact size, so it loaded whole
LOOKUP etc in sun2_f_m -> OK      init running, resolving /etc/rc.boot
LOOKUP rc.boot in sun2_f_m/etc -> OK
LOOKUP core in sun2_f_m -> OK     ...and something dying with cwd = /
CREATE sun2_f_m/core -> OK
SETATTR sun2_f_m/core size=0 -> OK
CREATE sun2_f_m/etc/utmp -> OK    init logging the death, then trying again
```

`CREATE core` + `SETATTR size=0` with no `WRITE` after it is a core dump that
started and stopped: the client truncated the file and never sent a byte. Every
request in that trace was answered `OK` — no NFS call failed — so whatever
kills the process is on the client side of the wire.

What is **not** in the trace is as informative. There is no `LOOKUP dev`, so
`/dev/console` was never opened; no `LOOKUP sh` anywhere, and no `READ rc.boot`,
so no shell was ever exec'd. The failure sits between resolving `/etc/rc.boot`
and opening the console, which is a handful of instructions into `init`.

Reading that trace did turn up one server-side defect, since fixed. `LOOKUP` was
minting file handles from `os.path.realpath()`, which resolves a trailing
symlink too: the client got NFLNK attributes together with the *target's*
handle, and the `READLINK` that necessarily followed failed with `NFSERR_IO`. A
SunOS root is held together by symlinks — `/bin`, `/lib`, `/usr/lib/ld.so`,
most of `/etc` — so nothing that walks a path through one could have worked.
`init` had not reached a symlink yet, which is the only reason it went unnoticed.
`selftest.sh` now `READLINK`s every symlink in the export root and compares the
answer with the disk.

Two related sharp edges went with it. A file handle now names one object and the
server never dereferences a final symlink (`O_NOFOLLOW` on every open), so a
link pointing out of the export — this root has three, e.g.
`/usr/ucb/newaliases -> /usr/lib/sendmail` — is handed to the client to resolve
in *its* namespace instead of being followed into this host's filesystem. And
`READ`/`WRITE` now refuse anything that is not a regular file, so the ~130
device nodes in `/dev` cannot be opened here by their host-side numbers. (Their
`rdev` values are reported correctly: SunOS's `(major << 8) | minor` and Linux's
encoding agree for every node in this tree.)

## NetBSD 2.0 with a real root

The other Sun-2 in the table boots NetBSD, and not the one the Sun-3s get:

```sh
sun2b	08:00:20:01:06:e1	192.168.0.124	sun2	netbsd2
```

NetBSD 10.1 still builds for sun2, and `netbsd-RAMDISK` boots on one, but its
userland does not fit a machine with 4MB of RAM and a 68010. NetBSD 2.0 is the
last release whose sun2 binaries are worth running on the hardware, and it is
old enough to live in the archive rather than on the mirrors, so it gets its
own release number and its own base URL:

```sh
NETBSD2_RELEASE=2.0
NETBSD2_KERNEL_SUN2=netbsd-DISKLESS
NETBSD2_SETS='base etc'
```

`DISKLESS` is the sun2 kernel built to mount its root over NFS. `RAMDISK` and
`INSTALL` carry their own root and would ignore the tree entirely; `GENERIC`
wants a local disk.

Two steps, neither of them privileged:

```sh
scripts/02-fetch-payload.sh          # bootyy, netboot, the kernel, base+etc
scripts/04-make-netbsd2-root.sh      # unpack them into nfsroot/sun2b
```

`archive.netbsd.org` puts a "trivial botcatcher" in front of the larger files —
a form asking what you are here for, answered with a `key=` parameter — so the
fetch says `NetBSD`, which is what it is here for. Everything is checked
against the release's own `MD5` files; the sets stay in `dist/` rather than
`payload/`, since `base.tgz` alone is 74MB and no PROM ever asks for it.

The boot chain is the NetBSD one described under "A Sun-2", an older release of
it: ND for `bootyy`, ND again for `netboot`, bootparams for `root`, then NFS
version 2 all the way. What is new is the far end — 177MB of root filesystem
instead of a single kernel.

### /dev without mknod

A root filesystem needs `/dev`, and `mknod(2)` is privileged. That is the
reason `root/make-sunos-root.sh` exists and needs `sudo`. It is not the reason
here.

NetBSD's own `dev/MAKEDEV` has a `-s` flag, for building a `/dev` while
cross-building as an ordinary user. It prints an `mtree(8)` specfile instead of
calling `mknod`:

```
./console type=char device=netbsd,0,0 mode=600 gid=0 uid=0
./sd0a type=block device=netbsd,7,0 mode=640 gid=5 uid=0
```

So `04-make-netbsd2-root.sh` keeps that file, puts an empty placeholder where
each of the 855 nodes belongs, and `nfs2d --devices` reports the one as the
other. Nothing is being faked that matters: a device node over NFS is a type
and a pair of numbers, which the client's own kernel acts on. No client ever
reads one — and `nfs2d` refuses to, since opening `/dev/console` on this side
would open *this host's* device of those numbers.

The numbers are NetBSD's, not Linux's. `sys/sys/types.h` puts twelve bits of
major at bit 8 and splits the minor between bits 31-20 and 7-0; for everything
in a Sun-2 `/dev` — major under 4096, minor under 256, the largest here being
63 — that comes to `(major << 8) | minor`, which is also how SunOS and Linux
spell it. `nfs2d` implements the full form anyway, so a large minor cannot
quietly turn into a small one.

Ownership is the same trick as the SunOS root: the tree comes out owned by
whoever unpacked it, and `--squash-to-root` reports it as root-owned, which
3295 of the 3330 files in `base.tgz` genuinely are. Device nodes are the
exception — the specfile says who owns each one, and that is a real answer, so
they are reported as themselves rather than squashed.

`tools/nfs-probe.py --check-devices` reads the same specfile and asks the
server about every node in it, comparing type, mode, uid, gid and rdev — the
specfile being the authority, not `nfs2d`:

```
GETATTR            -> 855 device nodes match MAKEDEV.spec, and READ of one is refused
```

### Which server answers MOUNT

A NetBSD 2.0 kernel does not simply ask for the NFS version it wants. From
`sys/nfs/nfs_boot.c`:

```c
	mntver = (argp->flags & NFSMNT_NFSV3) ? 3 : 2;
	do {
		error = krpc_portmap(mdsin, RPCPROG_MNT, mntver, ...);
		if (error) continue;
		error = krpc_call(mdsin, RPCPROG_MNT, mntver, RPCMNT_MOUNT, &m, NULL);
		if (error != EPROGMISMATCH) break;
	} while (--mntver >= 1);
```

It starts at version 3 — `options NFS_V2_ONLY` is commented out in
`sys/arch/sun2/conf/DISKLESS` — and walks downwards, but **only** on
`EPROGMISMATCH`. A version nobody registered gets port 0 from the portmapper,
a call to port 0 times out, and the boot ends there. So the fallback exists
only if something answers version 3 and says it does not speak it.

That matters because a `netbsd2` root cannot be served by unfs3 at all: its
`/dev` is 855 empty files, and only `nfs2d` knows they are anything else. A
client that reached it over NFSv3 would find an empty `/dev/console` and go no
further.

So when the table has a `netbsd2` client in it, `nfs2d` registers MOUNT
versions 1, 2 **and** 3 — answering 3 with `PROG_MISMATCH`, which is the true
answer and also the one that sends the kernel down to 2 — and `start.sh` does
not start `unfsd`:

```
==> a netbsd2 client is configured, so nfs2d answers every MOUNT
==>   version and unfsd would never be reached; not starting it.
```

Nothing is lost. Every NetBSD bootloader falls back by itself —
`sys/lib/libsa/nfs.c` retries with `RPCMNT_VER1` on any failure of the version
3 mount — so the Sun-3s carry on reading their kernels, over version 2 instead
of version 3. `selftest.sh` walks the versions the way the kernel does and
says where it lands:

```
MOUNT v3 on port 2049 -> PROG_MISMATCH, so a client falls back to v2
MOUNT v2 on port 2049 -> filehandle, so the root is mounted over NFS version 2
```

Take the `netbsd2` line out of the table and `unfsd` comes back, on 2050, with
version 3 as before.

### What it is configured with

`04-make-netbsd2-root.sh` touches four files in the unpacked tree, and says
which:

| file | why |
|---|---|
| `etc/rc.conf` | ships `rc_configured=NO`, which drops the boot to single user |
| `etc/fstab` | `/etc/rc.d/root` runs `mount /`, which needs a `/` entry |
| `etc/myname` | so the userland agrees with the name bootparams gave the kernel |
| `etc/hosts` | there is no DNS out here; both ends are named by hand |

Everything else is the release as shipped, including `root` with no password,
which is how NetBSD 2.0 comes and what makes the first console login possible.
`etc/ttys` already has a getty on `ttya`.

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
it with the same probes. It proves TFTP, ND, the portmap/bootparams exchange
and both NFS versions before anyone is asked for root:

```
  PASS  portmapper (100000) registered
  PASS  bootparam (100026) registered
  PASS  mountd (100005) registered
  PASS  nfs (100003) registered
  PASS  C0A80079 served, 23316 bytes, matches the netboot image
  PASS  broadcast read request answered from a usable source address
  PASS  08:00:20:01:06:e0: block 1 is sunos-sun2.bb
  PASS  08:00:20:01:06:e1: block 1 is netbsd2-bootyy
  PASS  bootparamd answered
  PASS  nfs2d served sun2b over NFSv2
```

It builds `nfs2d`'s arguments with the same `nfs2d_args` in `scripts/common.sh`
that `start.sh` uses, and stands `unfsd` down under the same rule, so what it
rehearses is what will actually run.

`scripts/selftest.sh` runs the same checks against the real daemons once they
are up.

RARP is the one step neither can cover: generating a RARP request needs a raw
socket of its own. Watch `log/rarpd.log` (`scripts/status.sh -f`) while the
Sun-3 boots.

## Reading the logs

Each daemon logs to `log/<name>.log`, and `scripts/status.sh -f` tails them
together — useful next to a serial console.

`nfs2d` stamps every line to the millisecond, which is not decoration: a
client retrying a `READ` whose answer never arrived looks exactly like a client
reading the same block twice, and only the gap between the two lines says which
it was.

```
2026-08-25 14:37:41.831 serving /…/nfsroot over NFSv2 on udp/2049, advertising tsize 1024
2026-08-25 14:37:44.402 192.168.0.124 LOOKUP vmunix in sun2b -> OK
```

### Rotating nfs2d's log

A boot writes thousands of lines and the file is never truncated, so rotate it
with `SIGHUP`:

```sh
mv log/nfs2d.log log/nfs2d.log.1
kill -HUP "$(cat run/nfs2d.pid)"
```

The daemon holds a descriptor, not a name, so without the signal it goes on
writing into the renamed file for ever. `start.sh` redirects its output, which
means `nfs2d` is never told where it is writing — so it reads the name once at
startup from `/proc/self/fd/2`, remembers it, and reopens *that* on `SIGHUP`.
Reading it again at rotation time would answer with the rotated name, which is
exactly the file not to go back to. It says which file it is holding when it
starts:

```
2026-08-25 14:37:41.827 logging to /…/log/nfs2d.log; SIGHUP reopens it
```

That line is also the check: if it is missing, stderr was not a file (a
terminal, or a pipe) and no handler was installed, so `SIGHUP` stays ignored
rather than becoming fatal — `start.sh` runs every daemon under `nohup`.
`logrotate` therefore needs no `copytruncate` here, and loses nothing.
`scripts/dryrun.sh` rehearses the rename-and-signal on its own copy.

The other daemons are not ours and have no such handler; for those,
`copytruncate` (or a restart) is the only option.

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

**unfs3 speaks NFSv3 only**, which is fine for a NetBSD bootloader -- it tries
MOUNT v3 first and falls back to v1 (`sys/lib/libsa/nfs.c`). A **SunOS 4.x**
boot program speaks NFSv2 and nothing else, and this host's kernel is built
with `CONFIG_NFSD_V2` unset, so neither unfs3 nor the in-kernel server can
serve it. That gap is filled by `tools/nfs2d.py`; see "SunOS on the Sun-2".

A NetBSD *kernel* mounting its root is a third case again: it starts at MOUNT
version 3 and falls back only on `PROG_MISMATCH`. See "Which server answers
MOUNT".

## A real NFS root

Milestones 1 to 3 deliberately ended at `netbsd-RAMDISK`, a kernel that carries
its own root filesystem, so that nothing had to solve the hard part until the
boot chain itself was proven.

The hard part was that a real root needs device nodes in `/dev` and correct
file ownership, and a server running as an ordinary user can neither `mknod`
nor `chown` -- which looked like a choice between running the NFS server as
root and giving up. It was neither. Ownership is reported rather than set
(`--squash-to-root`), and `/dev` is described rather than created
(`--devices`, from the specfile NetBSD's own `MAKEDEV -s` writes). Both are
things an NFS server is entitled to decide, because both are answers it sends
rather than state it holds.

There are two real roots here now, and neither needs privilege at runtime:

| | SunOS 4.0.3 | NetBSD 2.0 |
|---|---|---|
| unpacked by | `root/make-sunos-root.sh` | `scripts/04-make-netbsd2-root.sh` |
| needs root | **yes**, once, for `mknod` | no |
| `/dev` | 113 real device nodes | 855 placeholders + a specfile |
| ownership | `chown`ed to us, squashed back | never `chown`ed, squashed |

The SunOS one still needs root because its `/dev` comes out of a tar archive
with the nodes already in it, and there is no specfile to describe them
instead. See "A real SunOS root" and "NetBSD 2.0 with a real root".

## Layout

```
config/     the one file you edit
scripts/    everything unprivileged
root/       the one thing that is not
tools/      protocol probes (bp-probe.py, nfs-probe.py, tftp-bcast-probe.py,
            nd-probe.py) and nfs2d.py, an unprivileged NFSv2 server
            and sun3conf.py, which reads the client table for them
sbin/       the daemons; four carry capabilities
etc/        generated configuration
tftpboot/   what the PROM downloads over TFTP, and Sun-2 second stages
ndboot/     Sun-2 first stages, one per client, over ND
payload/    netboot and kernels as fetched
nfsroot/    what the client mounts
dist/ pkg/ src/   download cache, extracted .debs, local builds
run/ log/   pidfiles and logs
```
