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

It is read-only apart from the files named by --writable, which exist because
a diskless SunOS client swaps over NFS and so must be able to write its swap
file.  Everything else -- the kernels sitting in the same export -- still
returns NFSERR_ROFS, as does anything that would create or remove a name.

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

# The most we will ever put in one reply.  A client may ask for this much and
# must get all of it: a short READ is how NFSv2 says "end of file", so trimming
# a reply would silently truncate the file.
MAXDATA = 8192

# What we tell the client to ask for, in the tsize field of STATFS -- "the
# number of bytes the server would like to have in the data part of READ and
# WRITE requests" (RFC 1094).  Clients take it seriously.
#
# 1024 rather than 8192 because of the receiving end.  An 8192-byte reply is
# about 8.3KB of UDP, which IP splits into six fragments, five of them
# full-size frames, and a Sun-2's ie interface drops those with
#
#     ie0: giant packet
#
# leaving the client to retry forever and report the server as not responding.
# At 1024 the whole reply -- data, attributes, RPC and UDP and IP headers --
# is about 1160 bytes and travels as a single frame.  2048 would not do: it
# still needs two fragments, and the first of them is full size.
DEFAULT_TSIZE = 1024

FHSIZE = 32

# Only for the log.  A boot that stalls does so somewhere in here, and which
# call it stopped on is the whole question, so every one of them gets a line.
NFS2_PROCS = {
    0: "NULL", 1: "GETATTR", 2: "SETATTR", 3: "ROOT", 4: "LOOKUP",
    5: "READLINK", 6: "READ", 7: "WRITECACHE", 8: "WRITE", 9: "CREATE",
    10: "REMOVE", 11: "RENAME", 12: "LINK", 13: "SYMLINK", 14: "MKDIR",
    15: "RMDIR", 16: "READDIR", 17: "STATFS",
}
NFS2_ERRS = {
    0: "OK", 1: "PERM", 2: "NOENT", 5: "IO", 6: "NXIO", 13: "ACCES",
    17: "EXIST", 19: "NODEV", 20: "NOTDIR", 21: "ISDIR", 27: "FBIG",
    28: "NOSPC", 30: "ROFS", 63: "NAMETOOLONG", 66: "NOTEMPTY", 70: "STALE",
}

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


def pack_fattr(st, squash=False):
    """struct fattr -- seventeen 32-bit words, RFC 1094 section 2.3.5.

    squash reports everything as owned by root.  A SunOS root filesystem is
    root-owned throughout, but this server runs as an ordinary user and so must
    own the files on disk to be able to write them.  Reporting the on-disk
    owner would show the client a tree owned by a uid it has never heard of,
    with every setuid binary setuid to that uid instead of to root.
    """
    # NFSv2 has 32-bit sizes throughout.  A kernel is well under 4GB, but say
    # so rather than wrapping silently.
    size = min(st.st_size, 0xFFFFFFFF)
    return struct.pack(
        "!17I",
        nfs_type(st.st_mode),
        st.st_mode & 0xFFFF,
        st.st_nlink,
        0 if squash else st.st_uid & 0xFFFFFFFF,
        0 if squash else st.st_gid & 0xFFFFFFFF,
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
    def __init__(self, export, port, allow, debug, writable=(),
                 writable_trees=(), squash=False, tsize=DEFAULT_TSIZE):
        self.export = export
        self.port = port
        self.allow = allow
        self.debug = debug
        # Writes are allowed to these exact files and nothing else.  A diskless
        # SunOS client swaps over NFS, so its swap file has to be writable --
        # but the kernels beside it in the same export must not be.
        self.writable = {os.path.realpath(w) for w in writable}
        # Whole subtrees a client may write: its root filesystem.  Kept
        # separate from the single-file list so that a swap-only setup stays
        # exactly as narrow as it was.
        self.writable_trees = [os.path.realpath(t) for t in writable_trees]
        self.squash = squash
        self.tsize = tsize
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))

    def log(self, *a):
        if self.debug:
            print(*a, file=sys.stderr, flush=True)

    def is_writable(self, path):
        real = os.path.realpath(path)
        if real in self.writable:
            return True
        for tree in self.writable_trees:
            if real == tree or real.startswith(tree + os.sep):
                return True
        return False

    def attrs(self, st):
        return pack_fattr(st, self.squash)

    def shortname(self, path):
        """Paths relative to the export root, so a line stays readable."""
        root = self.export.root
        if path == root:
            return "/"
        if path.startswith(root + os.sep):
            return path[len(root) + 1:]
        return path

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
            # A directory for a root, but a client may also mount a file
            # path directly -- a swap file named by bootparams, for instance.
            if not self.export.contains(real) or not os.path.exists(real):
                self.log(f"{addr[0]} MNT {path} -> denied")
                return self.accepted(xid, SUCCESS,
                                     struct.pack("!I", NFSERR_ACCES))
            handle = self.export.remember(real)
            self.log(f"{addr[0]} MNT {path} -> ok")
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
        who = addr[0]
        op = NFS2_PROCS.get(proc, f"proc{proc}")

        if proc == 0:                                     # NULL
            self.log(f"{who} NULL")
            return self.accepted(xid, SUCCESS)

        def err(status, detail=""):
            self.log(f"{who} {op}{detail} -> {NFS2_ERRS.get(status, status)}")
            return self.accepted(xid, SUCCESS, struct.pack("!I", status))

        # Names may only be created and destroyed inside a writable tree.  A
        # swap-only export has none, so every one of these still answers ROFS
        # there -- the narrow case did not get wider.
        def dirop(dec):
            """diropargs: a directory handle and a name in it."""
            parent = self.export.resolve(dec.fixed(FHSIZE))
            name = dec.string().decode(errors="replace")
            if parent is None:
                return None, None, NFSERR_STALE
            if name in ("", ".", "..") or "/" in name:
                return None, None, NFSERR_ACCES
            target = os.path.join(parent, name)
            if not self.export.contains(target):
                return None, None, NFSERR_ACCES
            if not self.is_writable(target):
                return None, None, NFSERR_ROFS
            return parent, target, None

        def status_only(st):
            return self.accepted(xid, SUCCESS, struct.pack("!I", st))

        if proc in (9, 14):                               # CREATE, MKDIR
            parent, target, bad = dirop(d)
            if bad:
                return err(bad)
            mode = d.u32()                                # sattr.mode
            d.u32(); d.u32()                              # uid, gid: not ours
            size = d.u32()
            perm = 0o644 if mode == 0xFFFFFFFF else mode & 0o7777
            try:
                if proc == 14:
                    os.mkdir(target, perm if perm else 0o755)
                else:
                    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, perm)
                    if size not in (0, 0xFFFFFFFF):
                        os.ftruncate(fd, size)
                    os.close(fd)
                st = os.lstat(target)
            except OSError as exc:
                return err(errno_to_nfs(exc), f" {self.shortname(target)}")
            handle = self.export.remember(os.path.realpath(target))
            self.log(f"{who} {op} {self.shortname(target)} -> OK")
            return self.accepted(xid, SUCCESS,
                                 struct.pack("!I", NFS_OK) + handle + self.attrs(st))

        if proc in (10, 15):                              # REMOVE, RMDIR
            parent, target, bad = dirop(d)
            if bad:
                return err(bad)
            try:
                os.rmdir(target) if proc == 15 else os.remove(target)
            except OSError as exc:
                return err(errno_to_nfs(exc), f" {self.shortname(target)}")
            self.export.by_handle.pop(Export.handle_for(os.path.realpath(target)), None)
            self.log(f"{who} {op} {self.shortname(target)} -> OK")
            return status_only(NFS_OK)

        if proc == 11:                                    # RENAME
            _, src, bad = dirop(d)
            if bad:
                return err(bad)
            _, dst, bad = dirop(d)
            if bad:
                return err(bad)
            try:
                os.rename(src, dst)
            except OSError as exc:
                return err(errno_to_nfs(exc))
            self.export.remember(os.path.realpath(dst))
            self.log(f"{who} RENAME {self.shortname(src)} -> {self.shortname(dst)}")
            return status_only(NFS_OK)

        if proc == 12:                                    # LINK
            existing = self.export.resolve(d.fixed(FHSIZE))
            _, target, bad = dirop(d)
            if existing is None:
                return err(NFSERR_STALE)
            if bad:
                return err(bad)
            try:
                os.link(existing, target)
            except OSError as exc:
                return err(errno_to_nfs(exc))
            self.log(f"{who} LINK {self.shortname(target)} -> {self.shortname(existing)}")
            return status_only(NFS_OK)

        if proc == 13:                                    # SYMLINK
            _, target, bad = dirop(d)
            if bad:
                return err(bad)
            to = d.string().decode(errors="replace")
            try:
                os.symlink(to, target)
            except OSError as exc:
                return err(errno_to_nfs(exc), f" {self.shortname(target)}")
            self.export.remember(target)
            self.log(f"{who} SYMLINK {self.shortname(target)} -> {to}")
            return status_only(NFS_OK)

        if proc == 2:                                     # SETATTR
            path = self.export.resolve(d.fixed(FHSIZE))
            if path is None:
                return err(NFSERR_STALE)
            if not self.is_writable(path):
                return err(NFSERR_ROFS, f" {self.shortname(path)}")
            # sattr: mode, uid, gid, size, atime{sec,usec}, mtime{sec,usec}.
            # 0xFFFFFFFF means "leave alone", which is how a client asks to
            # change one field without knowing the others.
            mode, uid, gid, size = d.u32(), d.u32(), d.u32(), d.u32()
            try:
                if size != 0xFFFFFFFF:
                    with open(path, "r+b") as fh:
                        fh.truncate(size)
                    self.log(f"{who} SETATTR {self.shortname(path)} size={size} -> OK")
                else:
                    # mode, uid and gid we cannot honour as an ordinary user,
                    # and a swap file does not care.  Report success with the
                    # attributes unchanged rather than failing the boot.
                    self.log(f"{who} SETATTR {self.shortname(path)} "
                             f"(mode/uid/gid ignored) -> OK")
                st = os.lstat(path)
            except OSError as exc:
                return err(errno_to_nfs(exc))
            return self.accepted(xid, SUCCESS,
                                 struct.pack("!I", NFS_OK) + self.attrs(st))

        if proc == 8:                                     # WRITE
            path = self.export.resolve(d.fixed(FHSIZE))
            d.u32()                                       # beginoffset, unused
            offset = d.u32()
            d.u32()                                       # totalcount, unused
            data = d.string()
            if path is None:
                return err(NFSERR_STALE)
            if not self.is_writable(path):
                return err(NFSERR_ROFS, f" {self.shortname(path)}")
            try:
                with open(path, "r+b") as fh:
                    fh.seek(offset)
                    fh.write(data)
                st = os.lstat(path)
            except OSError as exc:
                return err(errno_to_nfs(exc))
            self.log(f"{who} WRITE {os.path.basename(path)} "
                     f"{offset}+{len(data)} -> OK")
            return self.accepted(xid, SUCCESS,
                                 struct.pack("!I", NFS_OK) + self.attrs(st))
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
            self.log(f"{who} GETATTR {self.shortname(path)} -> OK")
            return self.accepted(xid, SUCCESS,
                                 struct.pack("!I", NFS_OK) + self.attrs(st))

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
                return err(errno_to_nfs(exc), f" {name} in {self.shortname(path)}")
            handle = self.export.remember(os.path.realpath(target))
            self.log(f"{who} LOOKUP {name} in {self.shortname(path)} -> OK")
            return self.accepted(xid, SUCCESS,
                                 struct.pack("!I", NFS_OK) + handle + self.attrs(st))

        if proc == 5:                                     # READLINK
            path = self.export.resolve(d.fixed(FHSIZE))
            if path is None:
                return err(NFSERR_STALE)
            try:
                target = os.readlink(path)
            except OSError as exc:
                return err(errno_to_nfs(exc))
            self.log(f"{who} READLINK {self.shortname(path)} -> {target}")
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
            self.log(f"{who} READ {os.path.basename(path)} {offset}+{count} -> {len(chunk)}")
            return self.accepted(xid, SUCCESS,
                                 struct.pack("!I", NFS_OK) + self.attrs(st)
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
                try:
                    fileid = os.lstat(os.path.join(path, name)).st_ino & 0xFFFFFFFF
                except OSError:
                    fileid = index + 1
                entry = (struct.pack("!I", 1)             # another entry
                         + struct.pack("!I", fileid)
                         + pack_string(name)
                         + struct.pack("!I", index + 1))  # cookie
                if len(entry) > budget:
                    break
                body += entry
                budget -= len(entry)
                index += 1
            body += struct.pack("!II", 0, 1 if index >= len(names) else 0)
            self.log(f"{who} READDIR {self.shortname(path)} from {cookie} "
                     f"-> {index - cookie} entries")
            return self.accepted(xid, SUCCESS, body)

        if proc == 17:                                    # STATFS
            path = self.export.resolve(d.fixed(FHSIZE))
            if path is None:
                return err(NFSERR_STALE)
            try:
                vfs = os.statvfs(path)
            except OSError as exc:
                return err(errno_to_nfs(exc))
            self.log(f"{who} STATFS {self.shortname(path)} -> OK")
            cap = lambda v: min(v, 0x7FFFFFFF)
            return self.accepted(xid, SUCCESS, struct.pack(
                "!IIIIII", NFS_OK, self.tsize, vfs.f_bsize,
                cap(vfs.f_blocks), cap(vfs.f_bfree), cap(vfs.f_bavail)))

        self.log(f"{who} {op}: not implemented")
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
    ap.add_argument("--writable", action="append", default=[],
                    metavar="FILE",
                    help="allow writes to this exact file (repeatable).  "
                         "Everything else stays read-only; a diskless SunOS "
                         "client needs it for its swap file.")
    ap.add_argument("--writable-tree", action="append", default=[],
                    metavar="DIR",
                    help="allow writes anywhere under DIR (repeatable).  A "
                         "client with a real root filesystem needs this; a "
                         "client that only swaps does not.")
    ap.add_argument("--tsize", type=int, default=DEFAULT_TSIZE,
                    help=f"transfer size advertised in STATFS "
                         f"(default {DEFAULT_TSIZE}).  Raising it above about "
                         f"1400 makes replies fragment, which a Sun-2 reports "
                         f"as 'ie0: giant packet' and drops.")
    ap.add_argument("--squash-to-root", action="store_true",
                    help="report every file as owned by root.  A SunOS root is "
                         "root-owned throughout, but this server must own the "
                         "files on disk to be able to write them.")
    ap.add_argument("--no-register", action="store_true",
                    help="do not register with the portmapper")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    root = os.path.realpath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"{root} is not a directory")
    allow = {a for a in args.allow.split(",") if a}

    export = Export(root, debug=args.debug)
    server = Server(export, args.port, allow, args.debug, args.writable,
                    args.writable_tree, args.squash_to_root, args.tsize)
    for w in sorted(server.writable):
        print(f"writable file: {w}", file=sys.stderr)
    for t in server.writable_trees:
        print(f"writable tree: {t}", file=sys.stderr)
    if server.squash:
        print("reporting every file as owned by root", file=sys.stderr)

    if not args.no_register:
        for prog, vers, what in ((NFSPROG, NFSVERS, "nfs v2"),
                                 (MOUNTPROG, MOUNTVERS, "mount v1")):
            portmap(PMAPPROC_UNSET, prog, vers, 0)
            if not portmap(PMAPPROC_SET, prog, vers, args.port):
                raise SystemExit(f"the portmapper would not register {what}")
            print(f"registered {what} on udp/{args.port}", file=sys.stderr)

    print(f"serving {root} over NFSv2 on udp/{args.port}, "
          f"advertising tsize {args.tsize}", file=sys.stderr, flush=True)
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
