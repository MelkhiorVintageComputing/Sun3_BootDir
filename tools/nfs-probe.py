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

    print(f"\nOK: a SunOS boot program could mount {export} and read "
          f"{args.file} over NFSv2")
    return 0


NFSPROC2_WRITE = 8
NFSERR_ROFS = 30


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
