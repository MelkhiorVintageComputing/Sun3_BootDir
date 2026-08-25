#!/usr/bin/env python3
"""Read the kernel out of the NFS export exactly the way netboot does.

netboot's NFS client (sys/lib/libsa/nfs.c) tries MOUNT version 3 first and only
falls back to version 1 / NFSv2 if that fails.  That is what makes unfs3 --
which speaks NFSv3 and nothing else -- usable here, so it is worth confirming
rather than assuming.

The sequence is: portmap GETPORT for mountd, MNT the export, portmap GETPORT for
nfsd, LOOKUP the kernel, READ the first block.  If all of that works, the only
thing left between you and a booting kernel is the Sun-3 itself.

Usage:
    nfs-probe.py [--server ADDR] [--export PATH] [--file NAME]
"""

import argparse
import os
import socket
import struct
import sys

import sun3conf

PMAPPROG, PMAPVERS, PMAPPROC_GETPORT = 100000, 2, 3
MOUNTPROG, MOUNTVERS3, MOUNTPROC3_MNT, MOUNTPROC3_UMNT = 100005, 3, 1, 3
NFSPROG, NFSVERS3, NFSPROC3_LOOKUP, NFSPROC3_READ = 100003, 3, 3, 6
IPPROTO_UDP_RPC = 17

AUTH_NULL, AUTH_UNIX = 0, 1


def xdr_string(s):
    b = s.encode() if isinstance(s, str) else s
    return struct.pack("!I", len(b)) + b + b"\0" * (-len(b) % 4)


class Decoder:
    def __init__(self, buf):
        self.buf, self.off = buf, 0

    def u32(self):
        v, = struct.unpack_from("!I", self.buf, self.off)
        self.off += 4
        return v

    def u64(self):
        v, = struct.unpack_from("!Q", self.buf, self.off)
        self.off += 8
        return v

    def fixed(self, n):
        v = self.buf[self.off:self.off + n]
        self.off += n
        return v

    def opaque(self):
        n = self.u32()
        v = self.buf[self.off:self.off + n]
        self.off += n + (-n % 4)
        return v

    def skip_post_op_attr(self):
        if self.u32():
            self.off += 84  # fattr3


def auth_unix():
    host = socket.gethostname()[:255]
    body = (struct.pack("!I", 0) + xdr_string(host)
            + struct.pack("!III", os.getuid(), os.getgid(), 0))
    return struct.pack("!II", AUTH_UNIX, len(body)) + body


def rpc_call(sock, addr, prog, vers, proc, args, timeout=5.0):
    xid = struct.unpack("!I", os.urandom(4))[0]
    msg = (struct.pack("!IIIIII", xid, 0, 2, prog, vers, proc)
           + auth_unix() + struct.pack("!II", AUTH_NULL, 0) + args)
    sock.settimeout(timeout)
    sock.sendto(msg, addr)
    while True:
        try:
            reply, _ = sock.recvfrom(65536)
        except socket.timeout:
            raise TimeoutError(f"no reply from {addr[0]}:{addr[1]} "
                               f"(prog {prog} vers {vers} proc {proc})")
        if len(reply) < 24:
            continue
        d = Decoder(reply)
        if d.u32() != xid or d.u32() != 1:
            continue
        if d.u32() != 0:
            raise RuntimeError("RPC call rejected")
        d.u32()                     # verifier flavour
        verf_len = d.u32()          # ... and its body, which we skip
        d.off += verf_len
        status = d.u32()
        if status != 0:
            names = {1: "PROG_UNAVAIL", 2: "PROG_MISMATCH", 3: "PROC_UNAVAIL",
                     4: "GARBAGE_ARGS", 5: "SYSTEM_ERR"}
            raise RuntimeError(f"accept_stat={names.get(status, status)}")
        return reply[d.off:]


def getport(sock, server, prog, vers):
    args = struct.pack("!IIII", prog, vers, IPPROTO_UDP_RPC, 0)
    body = rpc_call(sock, (server, 111), PMAPPROG, PMAPVERS,
                    PMAPPROC_GETPORT, args)
    return Decoder(body).u32()


MOUNTVERS1, NFSVERS2 = 1, 2
NFSPROC2_LOOKUP, NFSPROC2_READ = 4, 6


def probe_v2(sock, args, export):
    """MOUNT v1 then NFS v2, which is all SunOS 4.0.3 can speak."""
    try:
        mport = getport(sock, args.server, MOUNTPROG, MOUNTVERS1)
    except Exception as exc:
        print(f"GETPORT mountd v1: FAILED: {exc}")
        return 1
    if mport == 0:
        print("GETPORT mountd v1: not registered -- is tools/nfs2d.py running?")
        return 1
    print(f"GETPORT mountd v1   -> port {mport}")

    reply = rpc_call(sock, (args.server, mport), MOUNTPROG, MOUNTVERS1, 1,
                     xdr_string(export))
    d = Decoder(reply)
    status = d.u32()
    if status != 0:
        print(f"MNT {export} -> status {status} (not exported to us?)")
        return 1
    fh = d.fixed(32)
    print(f"MNT {export} -> filehandle {len(fh)} bytes")

    if args.nfs_port:
        # SunOS never looks NFS up: 2049 is the well-known port and it just
        # sends there.  Skipping the lookup here is what makes this probe able
        # to catch a v3 server sitting on it.
        nport = args.nfs_port
        print(f"NFS v2 straight to port {nport} (no portmapper lookup)")
    else:
        try:
            nport = getport(sock, args.server, NFSPROG, NFSVERS2)
        except Exception as exc:
            print(f"GETPORT nfs v2: FAILED: {exc}")
            return 1
        if nport == 0:
            print("GETPORT nfs v2: not registered")
            return 1
        print(f"GETPORT nfs v2      -> port {nport}")

    reply = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS2,
                     NFSPROC2_LOOKUP, fh + xdr_string(args.file))
    d = Decoder(reply)
    status = d.u32()
    if status != 0:
        print(f"LOOKUP {args.file} -> NFSv2 error {status}")
        return 1
    filefh = d.fixed(32)
    d.off += 68                                   # struct fattr
    print(f"LOOKUP {args.file:<12} -> filehandle {len(filefh)} bytes")

    reply = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS2,
                     NFSPROC2_READ,
                     filefh + struct.pack("!III", 0, 1024, 1024))
    d = Decoder(reply)
    status = d.u32()
    if status != 0:
        print(f"READ {args.file} -> NFSv2 error {status}")
        return 1
    d.off += 68                                   # struct fattr
    data = d.opaque()
    print(f"READ {args.file} 0..1024 -> {len(data)} bytes, "
          f"first 16: {data[:16].hex()}")
    if not data:
        print("\n  read returned nothing")
        return 1
    if args.expect_writable or args.expect_readonly:
        if not check_writability(sock, args, fh, nport):
            return 1
    if args.check_symlinks:
        if not check_symlinks(sock, args, fh, nport, args.check_symlinks):
            return 1
    if args.check_devices:
        if not check_devices(sock, args, fh, nport, args.check_devices, export):
            return 1
    if args.check_mount_fallback:
        if not check_mount_fallback(sock, args, export):
            return 1

    print(f"\nOK: a SunOS boot program could mount {export} and read "
          f"{args.file} over NFSv2")
    return 0


NFSPROC2_WRITE = 8
NFSPROC2_READLINK = 5
NFSERR_ROFS = 30
NFSERR_IO = 5
NFBLK, NFCHR, NFLNK = 3, 4, 5


def v2_lookup(sock, args, dirfh, nport, name):
    reply = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS2,
                     NFSPROC2_LOOKUP, dirfh + xdr_string(name))
    d = Decoder(reply)
    if d.u32() != 0:
        return None
    return d.fixed(32)


def v2_write(sock, args, fh, nport, offset, data):
    reply = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS2,
                     NFSPROC2_WRITE,
                     fh + struct.pack("!III", 0, offset, len(data))
                     + xdr_string(data))
    return Decoder(reply).u32()


def check_writability(sock, args, dirfh, nport):
    """Is the export writable exactly where it should be?

    Both halves are non-destructive, which matters more than it sounds: this
    runs against the live server, and the client on the other end may be
    swapping to the very file being tested or running out of the very tree.

      writable  read some bytes and write the same bytes back.  A successful
                no-op write proves the permission without changing anything.
      readonly  write zero bytes.  The server decides whether to allow it
                before it has anything to store, so a refusal is just as
                informative and a success costs nothing.
    """
    if args.expect_writable:
        name = args.expect_writable
        fh = v2_lookup(sock, args, dirfh, nport, name)
        if fh is None:
            print(f"\n{name}: not present, so there is nothing to write to")
            return False
        reply = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS2,
                         NFSPROC2_READ, fh + struct.pack("!III", 0, 512, 512))
        d = Decoder(reply)
        if d.u32() != 0:
            print(f"\nREAD of {name} failed, so it cannot be write-tested")
            return False
        d.off += 68
        existing = d.opaque()
        status = v2_write(sock, args, fh, nport, 0, existing)
        if status != 0:
            print(f"\nWRITE {name} -> NFSv2 error {status}; a diskless client "
                  f"cannot swap to, or run from, a read-only file")
            return False
        print(f"WRITE {name:<10} -> {len(existing)} bytes rewritten unchanged")

    if args.expect_readonly:
        name = args.expect_readonly
        fh = v2_lookup(sock, args, dirfh, nport, name)
        if fh is None:
            print(f"\n{name}: not present")
            return False
        status = v2_write(sock, args, fh, nport, 0, b"")
        if status != NFSERR_ROFS:
            print(f"\nWRITE {name} -> status {status}, expected ROFS "
                  f"({NFSERR_ROFS}); the export is more writable than intended")
            return False
        print(f"WRITE {name:<10} -> refused with ROFS, as it should be")
    return True


def check_symlinks(sock, args, dirfh, nport, localdir):
    """Does every symlink in the export root read back as itself?

    A SunOS root is held together by symlinks -- /bin, /lib, /usr/lib/ld.so --
    and the client resolves them itself, so it must be able to READLINK the
    handle LOOKUP gave it.  A server that hands back the *target's* handle
    instead answers LOOKUP with NFLNK attributes and then fails the READLINK,
    which the client sees as an I/O error on a path that plainly exists.

    Reads the directory locally to decide what to ask for, so the check keeps
    up with whatever the root actually contains.
    """
    links = sorted(n for n in os.listdir(localdir)
                   if os.path.islink(os.path.join(localdir, n)))
    if not links:
        print("no symlinks in the export root to check")
        return True
    for name in links:
        want = os.readlink(os.path.join(localdir, name))
        reply = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS2,
                         NFSPROC2_LOOKUP, dirfh + xdr_string(name))
        d = Decoder(reply)
        if d.u32() != 0:
            print(f"\nLOOKUP {name}: failed, but it is a symlink on disk")
            return False
        fh = d.fixed(32)
        ftype = d.u32()
        if ftype != NFLNK:
            print(f"\nLOOKUP {name} -> type {ftype}, expected NFLNK ({NFLNK})")
            return False
        d.off += 64
        reply = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS2,
                         NFSPROC2_READLINK, fh)
        d = Decoder(reply)
        status = d.u32()
        if status != 0:
            print(f"\nREADLINK {name} -> NFSv2 error {status}; the client "
                  f"cannot resolve /{name} and every path through it fails")
            return False
        got = d.opaque().decode(errors="replace")
        if got != want:
            print(f"\nREADLINK {name} -> '{got}', on disk it is '{want}'")
            return False
    print(f"READLINK           -> {len(links)} symlinks in the export root "
          f"all read back correctly")
    return True


def check_mount_fallback(sock, args, export):
    """Walk the MOUNT versions the way a NetBSD 2.0 kernel does.

    sys/nfs/nfs_boot.c md_mount() asks the portmapper for MOUNT version 3,
    calls it, and only tries the next version down if that call comes back
    PROG_MISMATCH -- anything else, a version nobody registered included,
    ends the boot.  So for a client whose root only nfs2d can serve, nfs2d
    has to be the one holding version 3, and has to answer it with the
    mismatch that sends the kernel to version 2.

    This is that walk, and it fails where the kernel would.
    """
    for vers in (3, 2, 1):
        port = getport(sock, args.server, MOUNTPROG, vers)
        if port == 0:
            print(f"\nGETPORT mountd v{vers}: not registered.  A NetBSD kernel "
                  f"stops here rather than trying version {vers - 1}")
            return False
        try:
            reply = rpc_call(sock, (args.server, port), MOUNTPROG, vers, 1,
                             xdr_string(export))
        except RuntimeError as exc:
            if "PROG_MISMATCH" not in str(exc):
                print(f"\nMOUNT v{vers} -> {exc}, which is not a version "
                      f"a client can fall back from")
                return False
            print(f"MOUNT v{vers} on port {port} -> PROG_MISMATCH, "
                  f"so a client falls back to v{vers - 1}")
            continue
        status = Decoder(reply).u32()
        if status != 0:
            print(f"\nMOUNT v{vers} {export} -> status {status}")
            return False
        print(f"MOUNT v{vers} on port {port} -> filehandle, "
              f"so the root is mounted over NFS version 2")
        return True
    print("\nno MOUNT version answered")
    return False


def netbsd_makedev(major, minor):
    """sys/sys/types.h.  Spelled out here rather than imported from nfs2d, so
    that the check is an independent second opinion and not the same arithmetic
    agreeing with itself."""
    return (((major << 8) & 0x000FFF00)
            | ((minor << 12) & 0xFFF00000)
            | (minor & 0x000000FF))


def v2_walk(sock, args, dirfh, nport, relpath):
    """LOOKUP each component of relpath from dirfh."""
    fh = dirfh
    for part in relpath.split("/"):
        if part in ("", "."):
            continue
        fh = v2_lookup(sock, args, dirfh=fh, nport=nport, name=part)
        if fh is None:
            return None
    return fh


def check_devices(sock, args, dirfh, nport, specfile, export):
    """Does every node in the specfile come back as the device it describes?

    A root filesystem's /dev is the one part of it this server cannot put on
    disk: mknod(2) is privileged.  What is there instead is an empty file per
    node and the specfile NetBSD's own MAKEDEV -s wrote, and nfs2d reports the
    one as the other.  A client never reads a device node -- it takes the type
    and the numbers and acts on its own -- so type, mode, owner and rdev are
    the whole of what has to be right, and all four are checked here, for
    every node, against the specfile rather than against nfs2d.
    """
    kinds = {"char": NFCHR, "block": NFBLK}
    base = os.path.relpath(os.path.dirname(os.path.realpath(specfile)),
                           os.path.realpath(export))
    basefh = v2_walk(sock, args, dirfh, nport, base)
    if basefh is None:
        print(f"\nLOOKUP {base}: not there, so the specfile describes nothing")
        return False

    checked = 0
    sample = None
    with open(specfile) as fh_spec:
        for line in fh_spec:
            fields = line.split()
            if not fields or fields[0].startswith("#"):
                continue
            kw = dict(f.split("=", 1) for f in fields[1:] if "=" in f)
            want_type = kinds.get(kw.get("type"))
            if want_type is None or "device" not in kw:
                continue
            name = fields[0][2:] if fields[0].startswith("./") else fields[0]
            major, minor = (int(n, 0) for n in kw["device"].split(",")[-2:])
            want = (want_type, int(kw.get("mode", "600"), 8),
                    int(kw.get("uid", 0)), int(kw.get("gid", 0)),
                    netbsd_makedev(major, minor))

            fh = v2_walk(sock, args, basefh, nport, name)
            if fh is None:
                print(f"\nLOOKUP {base}/{name}: failed, but the specfile lists it")
                return False
            reply = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS2, 1,
                             fh)                          # GETATTR
            d = Decoder(reply)
            if d.u32() != 0:
                print(f"\nGETATTR {base}/{name}: failed")
                return False
            fa = struct.unpack("!17I", reply[d.off:d.off + 68])
            got = (fa[0], fa[1] & 0o7777, fa[3], fa[4], fa[7])
            if got != want:
                print(f"\n{base}/{name}: reported "
                      f"type={got[0]} mode={got[1]:o} uid={got[2]} gid={got[3]} "
                      f"rdev={got[4]:#x}, specfile says "
                      f"type={want[0]} mode={want[1]:o} uid={want[2]} gid={want[3]} "
                      f"rdev={want[4]:#x}")
                return False
            checked += 1
            if sample is None:
                sample = (name, fh)

    if not checked:
        print(f"\n{specfile} describes no device nodes at all")
        return False

    # And it must stay a name and a pair of numbers: reading one over NFS
    # would be this host's device of those numbers, not the client's.
    name, fh = sample
    reply = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS2,
                     NFSPROC2_READ, fh + struct.pack("!III", 0, 512, 512))
    status = Decoder(reply).u32()
    if status != NFSERR_IO:
        print(f"\nREAD {base}/{name} -> status {status}, expected IO "
              f"({NFSERR_IO}); a device node is not the server's to open")
        return False
    print(f"GETATTR            -> {checked} device nodes match "
          f"{os.path.basename(specfile)}, and READ of one is refused")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="127.0.0.1")
    ap.add_argument("--export", default=None,
                    help="export to mount (default: the client's NFS root)")
    ap.add_argument("--client", default=None,
                    help="which CLIENTS entry to use, by name or IP "
                         "(default: the first)")
    ap.add_argument("--expect-writable", metavar="NAME", default=None,
                    help="NFSv2 only: write a page to NAME and read it back, "
                         "which is what a diskless SunOS client does to its swap")
    ap.add_argument("--expect-readonly", metavar="NAME", default=None,
                    help="NFSv2 only: check a write to NAME is refused with "
                         "ROFS, so a kernel beside the swap file stays safe")
    ap.add_argument("--check-mount-fallback", action="store_true",
                    help="walk the MOUNT versions as a NetBSD kernel does -- "
                         "3, then 2, then 1 -- and check it reaches one this "
                         "server answers (NFSv2 only)")
    ap.add_argument("--check-devices", metavar="SPECFILE", default=None,
                    help="check every device node in this mtree(8) specfile "
                         "against what the server reports for it (NFSv2 only)")
    ap.add_argument("--check-symlinks", metavar="DIR", default=None,
                    help="NFSv2 only: LOOKUP and READLINK every symlink in the "
                         "export root and compare with DIR on disk -- a SunOS "
                         "root cannot be walked without working symlinks")
    ap.add_argument("--nfs-port", type=int, default=None,
                    help="send NFS straight to this port instead of asking the "
                         "portmapper -- what SunOS does, which is why 2049 has "
                         "to be the NFSv2 server")
    ap.add_argument("--nfs-version", type=int, choices=(2, 3), default=3,
                    help="3 for unfs3 and NetBSD (default); 2 for tools/nfs2d.py "
                         "and SunOS, which predates NFSv3 by six years")
    ap.add_argument("--file", default="netbsd",
                    help="file to look up inside it (default: netbsd)")
    args = ap.parse_args()

    bootdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    export = args.export or os.path.join(bootdir, "nfsroot",
                                         sun3conf.pick(args.client)["name"])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    if args.nfs_version == 2:
        return probe_v2(sock, args, export)

    try:
        mport = getport(sock, args.server, MOUNTPROG, MOUNTVERS3)
    except Exception as exc:
        print(f"GETPORT mountd v3: FAILED: {exc}")
        return 1
    if mport == 0:
        print("GETPORT mountd v3: not registered -- is unfsd running?")
        return 1
    print(f"GETPORT mountd v3   -> port {mport}")

    try:
        body = rpc_call(sock, (args.server, mport), MOUNTPROG, MOUNTVERS3,
                        MOUNTPROC3_MNT, xdr_string(export))
    except Exception as exc:
        print(f"MNT {export}: FAILED: {exc}")
        return 1
    d = Decoder(body)
    status = d.u32()
    if status != 0:
        print(f"MNT {export}: mountstat3={status} "
              "(13 = access denied: check etc/exports)")
        return 1
    fh = d.opaque()
    print(f"MNT {export} -> filehandle {len(fh)} bytes")

    nport = getport(sock, args.server, NFSPROG, NFSVERS3)
    if nport == 0:
        print("GETPORT nfs v3: not registered")
        return 1
    print(f"GETPORT nfs v3      -> port {nport}")

    lookup = xdr_string(fh) + xdr_string(args.file)
    body = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS3,
                    NFSPROC3_LOOKUP, lookup)
    d = Decoder(body)
    status = d.u32()
    if status != 0:
        print(f"LOOKUP {args.file}: nfsstat3={status} (2 = no such file)")
        return 1
    kernel_fh = d.opaque()
    print(f"LOOKUP {args.file}       -> filehandle {len(kernel_fh)} bytes")

    read = xdr_string(kernel_fh) + struct.pack("!QI", 0, 1024)
    body = rpc_call(sock, (args.server, nport), NFSPROG, NFSVERS3,
                    NFSPROC3_READ, read)
    d = Decoder(body)
    status = d.u32()
    if status != 0:
        print(f"READ {args.file}: nfsstat3={status}")
        return 1
    d.skip_post_op_attr()
    count = d.u32()
    d.u32()             # eof
    data = d.opaque()
    print(f"READ {args.file} 0..1024 -> {count} bytes, first 16: {data[:16].hex()}")

    rpc_call(sock, (args.server, mport), MOUNTPROG, MOUNTVERS3,
             MOUNTPROC3_UMNT, xdr_string(export))

    print("\nOK: netboot can mount the export and read the kernel over NFSv3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
