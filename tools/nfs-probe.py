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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="127.0.0.1")
    ap.add_argument("--export", default=None,
                    help="export to mount (default: the client's NFS root)")
    ap.add_argument("--client", default=None,
                    help="which CLIENTS entry to use, by name or IP "
                         "(default: the first)")
    ap.add_argument("--file", default="netbsd",
                    help="file to look up inside it (default: netbsd)")
    args = ap.parse_args()

    bootdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    export = args.export or os.path.join(bootdir, "nfsroot",
                                         sun3conf.pick(args.client)["name"])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

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
