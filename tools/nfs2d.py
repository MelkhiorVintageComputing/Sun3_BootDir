#!/usr/bin/env python3
"""A read-only NFS version 2 server, over UDP, for booting SunOS.

Why this exists: SunOS 4.0.3 is from 1989 and speaks NFSv2, which came out in
1985.  NFSv3 is from 1995.  `boot.sun2` and the SunOS kernel can therefore not
read a kernel out of unfs3, which serves NFSv3 and nothing else, and this
host's kernel is built without CONFIG_NFSD_V2 so the in-kernel server cannot
fill the gap either -- and would want root in any case.

So this serves NFS version 2 (program 100003) and MOUNT version 1 (100005)
alongside unfs3 rather than in place of it: different version numbers register
independently with the portmapper, so a NetBSD client keeps using unfs3 over
NFSv3 and a SunOS client comes here.  Both can boot at the same time.

It is deliberately read-only.  Everything that would modify the export returns
NFSERR_ROFS.  That is enough to load a kernel, which is what netbooting needs;
it is *not* enough for SunOS to then come up multiuser, which needs a writable
root filesystem this does not have.

Runs as an ordinary user.  Nothing here needs a privileged port: the client
finds us through the portmapper.

    nfs2d.py --root DIR [--port N] [--allow IP,IP] [--no-register] [--debug]
"""

import argparse
import errno
import os
import socket
import stat
import struct
import sys
import time

PMAPPROG, PMAPVERS = 100000, 2
PMAPPROC_SET, PMAPPROC_UNSET = 1, 2
IPPROTO_UDP_RPC = 17

MOUNTPROG, MOUNTVERS = 100005, 1
NFSPROG, NFSVERS = 100003, 2

# RPC
CALL, REPLY = 0, 1
MSG_ACCEPTED, MSG_DENIED = 0, 1
SUCCESS, PROG_UNAVAIL, PROG_MISMATCH, PROC_UNAVAIL, GARBAGE_ARGS = 0, 1, 2, 3, 4
AUTH_NULL = 0

# NFSv2 status
NFS_OK = 0
NFSERR_PERM, NFSERR_NOENT, NFSERR_IO, NFSERR_NXIO = 1, 2, 5, 6
NFSERR_ACCES, NFSERR_EXIST, NFSERR_NODEV, NFSERR_NOTDIR = 13, 17, 19, 20
NFSERR_ISDIR, NFSERR_FBIG, NFSERR_NOSPC, NFSERR_ROFS = 21, 27, 28, 30
NFSERR_NAMETOOLONG, NFSERR_NOTEMPTY, NFSERR_STALE = 63, 66, 70

# NFSv2 file types
NFNON, NFREG, NFDIR, NFBLK, NFCHR, NFLNK = 0, 1, 2, 3, 4, 5

MAXDATA = 8192
FHSIZE = 32

ERRNO_TO_NFS = {
    errno.EPERM: NFSERR_PERM,
    errno.ENOENT: NFSERR_NOENT,
    errno.EIO: NFSERR_IO,
    errno.ENXIO: NFSERR_NXIO,
    errno.EACCES: NFSERR_ACCES,
    errno.EEXIST: NFSERR_EXIST,
    errno.ENODEV: NFSERR_NODEV,
    errno.ENOTDIR: NFSERR_NOTDIR,
    errno.EISDIR: NFSERR_ISDIR,
    errno.ENAMETOOLONG: NFSERR_NAMETOOLONG,
    errno.ENOTEMPTY: NFSERR_NOTEMPTY,
}


def pack_string(b):
    if isinstance(b, str):
        b = b.encode()
    return struct.pack("!I", len(b)) + b + b"\0" * (-len(b) % 4)


class Decoder:
    def __init__(self, buf, off=0):
        self.buf, self.off = buf, off

    def u32(self):
        v, = struct.unpack_from("!I", self.buf, self.off)
        self.off += 4
        return v

    def fixed(self, n):
        v = self.buf[self.off:self.off + n]
        self.off += n
        return v

    def string(self):
        n = self.u32()
        v = self.buf[self.off:self.off + n]
        self.off += n + (-n % 4)
        return v

    def skip_auth(self):
        """Step over one opaque_auth: flavour, then a counted body.

        The length must land in a variable first.  "self.off += self.u32()"
        reads self.off before the call that advances it, so the assignment
        puts it back four bytes -- which decodes as plausible nonsense rather
        than an error.
        """
        self.u32()
        length = self.u32()
        self.off += length


class Export:
    """The directory tree we serve, and the file handles that name it.

    Handles are derived from the path, so they survive a restart -- a boot that
    spans one is unusual but a stale handle in the middle of loading a kernel
    would be a miserable thing to debug.  The reverse map is rebuilt by walking
    the tree, which is cheap: a client root holds a kernel and little else.
    """

    def __init__(self, root, debug=False):
        self.root = os.path.realpath(root)
        self.debug = debug
        self.by_handle = {}
        self.scan()

    @staticmethod
    def handle_for(path):
        import hashlib
        return hashlib.sha256(path.encode()).digest()[:FHSIZE]

    def scan(self):
        self.by_handle = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            self.remember(dirpath)
            for name in filenames + dirnames:
                self.remember(os.path.join(dirpath, name))
        self.remember(self.root)

    def remember(self, path):
        self.by_handle[self.handle_for(path)] = path
        return self.handle_for(path)

    def resolve(self, handle):
        """Path for a handle, rescanning once in case the tree changed."""
        path = self.by_handle.get(handle)
        if path is None:
            self.scan()
            path = self.by_handle.get(handle)
        if path is None:
            return None
        if not self.contains(path):
            return None
        return path

    def contains(self, path):
        real = os.path.realpath(path)
        return real == self.root or real.startswith(self.root + os.sep)


def nfs_type(mode):
    if stat.S_ISREG(mode):
        return NFREG
    if stat.S_ISDIR(mode):
        return NFDIR
    if stat.S_ISLNK(mode):
        return NFLNK
    if stat.S_ISBLK(mode):
        return NFBLK
    if stat.S_ISCHR(mode):
        return NFCHR
    return NFNON


def pack_fattr(st):
    """struct fattr -- seventeen 32-bit words, RFC 1094 section 2.3.5."""
    # NFSv2 has 32-bit sizes throughout.  A kernel is well under 4GB, but say
    # so rather than wrapping silently.
    size = min(st.st_size, 0xFFFFFFFF)
    return struct.pack(
        "!17I",
        nfs_type(st.st_mode),
        st.st_mode & 0xFFFF,
        st.st_nlink,
        st.st_uid & 0xFFFFFFFF,
        st.st_gid & 0xFFFFFFFF,
        size,
        512,                                  # blocksize
        st.st_rdev & 0xFFFFFFFF,
        st.st_blocks & 0xFFFFFFFF,
        st.st_dev & 0xFFFFFFFF,               # fsid
        st.st_ino & 0xFFFFFFFF,               # fileid
        int(st.st_atime), 0,
        int(st.st_mtime), 0,
        int(st.st_ctime), 0,
    )


def errno_to_nfs(exc):
    return ERRNO_TO_NFS.get(getattr(exc, "errno", None), NFSERR_IO)


class Server:
    def __init__(self, export, port, allow, debug):
        self.export = export
        self.port = port
        self.allow = allow
        self.debug = debug
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))

    def log(self, *a):
        if self.debug:
            print(*a, file=sys.stderr, flush=True)

    # ---------------------------------------------------------------- RPC
    def serve_forever(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65536)
            except InterruptedError:
                continue
            except OSError as exc:
                self.log("recvfrom:", exc)
                continue
            if self.allow and addr[0] not in self.allow:
                self.log(f"ignoring {addr[0]}: not in --allow")
                continue
            try:
                reply = self.handle(data, addr)
            except Exception as exc:                      # never die on a client
                self.log("error handling a call:", repr(exc))
                reply = None
            if reply:
                try:
                    self.sock.sendto(reply, addr)
                except OSError as exc:
                    self.log("sendto:", exc)

    @staticmethod
    def accepted(xid, status, body=b""):
        return (struct.pack("!IIII", xid, REPLY, MSG_ACCEPTED, AUTH_NULL)
                + struct.pack("!II", 0, status) + body)

    def handle(self, data, addr):
        d = Decoder(data)
        xid = d.u32()
        if d.u32() != CALL:
            return None
        if d.u32() != 2:                                  # RPC version
            return None
        prog, vers, proc = d.u32(), d.u32(), d.u32()
        d.skip_auth()                                     # credentials
        d.skip_auth()                                     # verifier

        if prog == NFSPROG:
            if vers != NFSVERS:
                return self.accepted(xid, PROG_MISMATCH,
                                     struct.pack("!II", NFSVERS, NFSVERS))
            return self.nfs(xid, proc, d, addr)
        if prog == MOUNTPROG:
            if vers != MOUNTVERS:
                return self.accepted(xid, PROG_MISMATCH,
                                     struct.pack("!II", MOUNTVERS, MOUNTVERS))
            return self.mount(xid, proc, d, addr)
        return self.accepted(xid, PROG_UNAVAIL)

    # -------------------------------------------------------------- MOUNT v1
    def mount(self, xid, proc, d, addr):
        if proc == 0:                                     # NULL
            return self.accepted(xid, SUCCESS)
        if proc == 1:                                     # MNT
            path = d.string().decode(errors="replace")
            real = os.path.realpath(path)
            if not self.export.contains(real) or not os.path.isdir(real):
                self.log(f"MNT {path} from {addr[0]}: denied")
                return self.accepted(xid, SUCCESS,
                                     struct.pack("!I", NFSERR_ACCES))
            handle = self.export.remember(real)
            self.log(f"MNT {path} from {addr[0]}: ok")
            return self.accepted(xid, SUCCESS, struct.pack("!I", 0) + handle)
        if proc in (3, 4):                                # UMNT, UMNTALL
            return self.accepted(xid, SUCCESS)
        if proc == 5:                                     # EXPORT
            body = (struct.pack("!I", 1) + pack_string(self.export.root)
                    + struct.pack("!I", 0)                # no groups
                    + struct.pack("!I", 0))               # end of list
            return self.accepted(xid, SUCCESS, body)
        if proc == 2:                                     # DUMP
            return self.accepted(xid, SUCCESS, struct.pack("!I", 0))
        return self.accepted(xid, PROC_UNAVAIL)

    # --------------------------------------------------------------- NFS v2
    def nfs(self, xid, proc, d, addr):
        if proc == 0:                                     # NULL
            return self.accepted(xid, SUCCESS)

        def err(status):
            return self.accepted(xid, SUCCESS, struct.pack("!I", status))

        # Everything that writes.  Saying ROFS plainly beats a timeout.
        if proc in (2, 8, 9, 10, 11, 12, 13, 14, 15):
            self.log(f"proc {proc} from {addr[0]}: read-only export")
            return err(NFSERR_ROFS)
        if proc == 7:                                     # WRITECACHE, a no-op
            return self.accepted(xid, SUCCESS)

        if proc == 1:                                     # GETATTR
            path = self.export.resolve(d.fixed(FHSIZE))
            if path is None:
                return err(NFSERR_STALE)
            try:
                st = os.lstat(path)
            except OSError as exc:
                return err(errno_to_nfs(exc))
            return self.accepted(xid, SUCCESS,
                                 struct.pack("!I", NFS_OK) + pack_fattr(st))

        if proc == 4:                                     # LOOKUP
            path = self.export.resolve(d.fixed(FHSIZE))
            name = d.string().decode(errors="replace")
            if path is None:
                return err(NFSERR_STALE)
            if name in ("", "."):
                target = path
            elif name == "..":
                target = os.path.dirname(path)
            else:
                if "/" in name:
                    return err(NFSERR_ACCES)
                target = os.path.join(path, name)
            if not self.export.contains(target):
                # ".." out of the export lands the client back at the root,
                # which is what an exported filesystem looks like from inside.
                target = self.export.root
            try:
                st = os.lstat(target)
            except OSError as exc:
                self.log(f"LOOKUP {name} in {path}: {exc.strerror}")
                return err(errno_to_nfs(exc))
            handle = self.export.remember(os.path.realpath(target))
            self.log(f"LOOKUP {name} -> {target}")
            return self.accepted(xid, SUCCESS,
                                 struct.pack("!I", NFS_OK) + handle + pack_fattr(st))

        if proc == 5:                                     # READLINK
            path = self.export.resolve(d.fixed(FHSIZE))
            if path is None:
                return err(NFSERR_STALE)
            try:
                target = os.readlink(path)
            except OSError as exc:
                return err(errno_to_nfs(exc))
            return self.accepted(xid, SUCCESS,
                                 struct.pack("!I", NFS_OK) + pack_string(target))

        if proc == 6:                                     # READ
            path = self.export.resolve(d.fixed(FHSIZE))
            offset, count = d.u32(), d.u32()
            d.u32()                                       # totalcount, unused
            if path is None:
                return err(NFSERR_STALE)
            count = min(count, MAXDATA)
            try:
                st = os.lstat(path)
                if stat.S_ISDIR(st.st_mode):
                    return err(NFSERR_ISDIR)
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read(count)
            except OSError as exc:
                return err(errno_to_nfs(exc))
            self.log(f"READ {os.path.basename(path)} {offset}+{count} -> {len(chunk)}")
            return self.accepted(xid, SUCCESS,
                                 struct.pack("!I", NFS_OK) + pack_fattr(st)
                                 + pack_string(chunk))

        if proc == 16:                                    # READDIR
            path = self.export.resolve(d.fixed(FHSIZE))
            cookie = d.u32()
            count = d.u32()
            if path is None:
                return err(NFSERR_STALE)
            try:
                names = sorted(os.listdir(path))
            except OSError as exc:
                return err(errno_to_nfs(exc))
            names = [".", ".."] + names
            body = struct.pack("!I", NFS_OK)
            # Budget the reply against the count the client asked for, leaving
            # room for the status, the end-of-list marker and the eof flag.
            budget = min(count, MAXDATA) - 16
            index = cookie
            while index < len(names):
                name = names[index]
                entry = (struct.pack("!I", 1)             # another entry
                         + struct.pack("!I", index + 1)   # fileid, near enough
                         + pack_string(name)
                         + struct.pack("!I", index + 1))  # cookie
                if len(entry) > budget:
                    break
                body += entry
                budget -= len(entry)
                index += 1
            body += struct.pack("!II", 0, 1 if index >= len(names) else 0)
            return self.accepted(xid, SUCCESS, body)

        if proc == 17:                                    # STATFS
            path = self.export.resolve(d.fixed(FHSIZE))
            if path is None:
                return err(NFSERR_STALE)
            try:
                vfs = os.statvfs(path)
            except OSError as exc:
                return err(errno_to_nfs(exc))
            cap = lambda v: min(v, 0x7FFFFFFF)
            return self.accepted(xid, SUCCESS, struct.pack(
                "!IIIIII", NFS_OK, MAXDATA, vfs.f_bsize,
                cap(vfs.f_blocks), cap(vfs.f_bfree), cap(vfs.f_bavail)))

        return self.accepted(xid, PROC_UNAVAIL)


def portmap(proc, prog, vers, port):
    """PMAPPROC_SET/UNSET against the local portmapper."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    xid = int.from_bytes(os.urandom(4), "big")
    call = (struct.pack("!IIIIII", xid, CALL, 2, PMAPPROG, PMAPVERS, proc)
            + struct.pack("!IIII", AUTH_NULL, 0, AUTH_NULL, 0)
            + struct.pack("!IIII", prog, vers, IPPROTO_UDP_RPC, port))
    try:
        sock.sendto(call, ("127.0.0.1", 111))
        data, _ = sock.recvfrom(65536)
    except OSError as exc:
        raise SystemExit(f"could not reach the portmapper on 127.0.0.1:111: {exc}")
    finally:
        sock.close()
    d = Decoder(data)
    d.u32()                                               # xid
    d.u32()                                               # REPLY
    d.u32()                                               # MSG_ACCEPTED
    d.skip_auth()                                         # verifier
    if d.u32() != SUCCESS:
        raise SystemExit("the portmapper rejected the registration")
    return d.u32() != 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True,
                    help="directory tree to serve; any directory inside it can be mounted")
    ap.add_argument("--port", type=int, default=2050,
                    help="UDP port (default 2050; 2049 belongs to unfs3)")
    ap.add_argument("--allow", default="",
                    help="comma-separated client addresses (default: any)")
    ap.add_argument("--no-register", action="store_true",
                    help="do not register with the portmapper")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    root = os.path.realpath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"{root} is not a directory")
    allow = {a for a in args.allow.split(",") if a}

    export = Export(root, debug=args.debug)
    server = Server(export, args.port, allow, args.debug)

    if not args.no_register:
        for prog, vers, what in ((NFSPROG, NFSVERS, "nfs v2"),
                                 (MOUNTPROG, MOUNTVERS, "mount v1")):
            portmap(PMAPPROC_UNSET, prog, vers, 0)
            if not portmap(PMAPPROC_SET, prog, vers, args.port):
                raise SystemExit(f"the portmapper would not register {what}")
            print(f"registered {what} on udp/{args.port}", file=sys.stderr)

    print(f"serving {root} read-only over NFSv2 on udp/{args.port}",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if not args.no_register:
            for prog, vers in ((NFSPROG, NFSVERS), (MOUNTPROG, MOUNTVERS)):
                try:
                    portmap(PMAPPROC_UNSET, prog, vers, 0)
                except SystemExit:
                    pass


if __name__ == "__main__":
    main()
