#!/usr/bin/env python3
"""An NFS version 2 server, over UDP, for booting SunOS and old NetBSD.

Why this exists: SunOS 4.0.3 is from 1989 and speaks NFSv2, which came out in
1985.  NFSv3 is from 1995.  `boot.sun2` and the SunOS kernel can therefore not
read a kernel out of unfs3, which serves NFSv3 and nothing else, and this
host's kernel is built without CONFIG_NFSD_V2 so the in-kernel server cannot
fill the gap either -- and would want root in any case.  A NetBSD/sun2
bootstrap of any vintage is in the same position: its libsa NFS client is
version 2 only.

So this serves NFS version 2 (program 100003) and the MOUNT protocol (100005)
alongside unfs3 rather than in place of it: different version numbers register
independently with the portmapper, so a NetBSD client that wants NFSv3 keeps
using unfs3 and a version 2 client comes here.  Both can boot at the same
time.  --mount-versions changes that when a client needs it to.

Writes go only where they are allowed to: --writable names a single file (a
diskless SunOS client swaps over NFS and so must be able to write its swap
file), --writable-tree a whole root filesystem.  Everything else returns
NFSERR_ROFS, as does anything that would create or remove a name.

A real root filesystem needs a /dev, and mknod(2) is privileged.  --devices
takes the specfile NetBSD's own dev/MAKEDEV writes with -s and reports the
ordinary placeholder files named in it as the device nodes they stand for.
Nothing ever reads a device node over NFS -- it is a name and a pair of
numbers for the client's own kernel -- so that is the whole of what a client
needs.

Runs as an ordinary user.  Nothing here needs a privileged port: the client
finds us through the portmapper.

Every line it writes is stamped to the millisecond, because a client retrying
a READ whose answer never arrived looks exactly like a client reading the same
block twice, and only the gap between the two lines says which.  To rotate the
log, rename it and send SIGHUP: this process is never told where its output is
going -- start.sh redirects it -- so it reads the name once from /proc at
startup and reopens that.

    nfs2d.py --root DIR [--port N] [--allow IP,IP] [--no-register] [--debug]
"""

import argparse
import errno
import os
import signal
import socket
import stat
import struct
import sys
import time

PMAPPROG, PMAPVERS = 100000, 2
PMAPPROC_SET, PMAPPROC_UNSET = 1, 2
IPPROTO_UDP_RPC = 17

MOUNTPROG = 100005
# Both versions of the MOUNT protocol we speak.  Version 1 is RFC 1094;
# version 2 (MOUNTVERS_POSIX) is the same thing plus a PATHCONF procedure,
# which nothing here is asked for.  Answering both matters because a NetBSD
# kernel walks the versions downwards -- 3, then 2, then 1 -- and gives up the
# moment one of them fails to answer at all, rather than trying the next.
MOUNT_VERSIONS = (1, 2)
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
# 1024 keeps a whole reply -- data, attributes, RPC and UDP and IP headers --
# at about 1160 bytes, inside a single Ethernet frame, which is a reasonable
# thing to ask of a 1989 client on a 10Mbit wire.
#
# It is advisory, and a SunOS kernel mounting its root ignores it: it asks for
# 8192 whatever we say here.  Do not reach for this to work around a client
# that cannot receive the resulting fragments -- see README, "ie0: giant
# packet".
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


def stamp():
    """Wall-clock time, to the millisecond.

    Milliseconds because what this log is usually read for is timing: a client
    retrying a READ whose answer never arrived looks exactly like a client
    reading the same block twice, and only the gap between the two lines says
    which of them it was.
    """
    now = time.time()
    return (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            + ".%03d" % (now % 1 * 1000))


def note(*a):
    """Every line this server writes goes through here: stamped, on stderr,
    and flushed, so that a log which stops tells you when it stopped."""
    print(stamp(), *a, file=sys.stderr, flush=True)


def logfile():
    """The path stderr is pointed at, if it is pointed at a file at all.

    scripts/start.sh redirects it, so this process is never told the name.
    /proc will say it once, at startup.  Asking again after a rotation would
    answer with the *rotated* name, which is precisely the file not to go back
    to, so the answer is remembered rather than looked up when needed.
    """
    try:
        if not stat.S_ISREG(os.fstat(2).st_mode):
            return None
        path = os.readlink("/proc/self/fd/2")
    except OSError:
        return None
    # Linux marks an unlinked target; the name is still where to reopen.
    suffix = " (deleted)"
    return path[:-len(suffix)] if path.endswith(suffix) else path


def reopen_log(path):
    """Point stderr back at path, whatever is there now.

    This is the whole of what a rotation needs from us: the log is renamed out
    from under the daemon, which goes on writing into the renamed inode until
    it is told to look at the name again.  dup2 onto descriptor 2 rather than
    replacing sys.stderr, so that every reference to it follows -- ours, and
    anything the interpreter writes there on its own account.
    """
    sys.stderr.flush()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    os.dup2(fd, 2)
    os.close(fd)


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
        # Handles minted for a name that has since been renamed away.  A real
        # NFS server derives a handle from the inode, so a rename does not
        # disturb it: the client goes on writing through the handle it opened
        # with and the bytes land in the renamed file.  Ours are derived from
        # the path, so the rename has to be recorded or the handle names
        # nothing -- which is exactly what a compiler does, writing l.outaNNN
        # and renaming it over the target while the descriptor is still open.
        self.aliases = {}
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
        # Re-applied last: a walk of the tree cannot rediscover a handle whose
        # name no longer exists, and a rescan happens on any handle miss --
        # which is precisely when a renamed-away handle is being looked up.
        self.by_handle.update(self.aliases)

    def remember(self, path):
        self.by_handle[self.handle_for(path)] = path
        return self.handle_for(path)

    def renamed(self, src, dst):
        """Keep the handle minted for src naming the object, now called dst.

        Also re-points any handle already aliased to src, so that a chain of
        renames -- a to b, then b to c -- leaves every handle in it pointing
        at c rather than at a name that has moved on again.
        """
        src, dst = canon(src), canon(dst)
        for handle, path in self.aliases.items():
            if path == src:
                self.aliases[handle] = dst
        self.aliases[self.handle_for(src)] = dst
        self.by_handle.update(self.aliases)
        return self.remember(dst)

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
        # canon(), not realpath(): a symlink belongs to the export whatever it
        # points at, because the client -- not the server -- resolves it, in
        # its own namespace.  /usr/ucb/newaliases -> /usr/lib/sendmail means
        # the client's /usr/lib/sendmail.  Nothing here dereferences a final
        # symlink (every open below passes O_NOFOLLOW), so answering with the
        # link itself cannot reach outside the tree.
        real = canon(path)
        return real == self.root or real.startswith(self.root + os.sep)


def canon(path):
    """The path a file handle is minted from: the parent resolved, the last
    component left exactly as it is.

    os.path.realpath() would resolve a trailing symlink too, and then LOOKUP
    would answer with the symlink's attributes but the *target's* handle.  The
    client, seeing NFLNK, calls READLINK on that handle -- and READLINK on the
    target fails, because the target is not a symlink.  A SunOS root is full of
    symlinks (/bin, /lib, /usr/lib/ld.so), so that is not a corner case.
    """
    parent, name = os.path.split(path)
    if name in ("", ".", ".."):
        return os.path.realpath(path)
    return os.path.join(os.path.realpath(parent), name)


def opennofollow(path, mode):
    """open(), but never through a symlink.

    A file handle names one object.  If the object is a symlink the client
    resolves it itself and comes back with a handle for whatever it found, so
    the server has no business dereferencing one -- and doing so is how a link
    pointing out of the export would turn into a read of a host file.
    """
    flags = os.O_RDONLY if mode == "rb" else os.O_RDWR
    return os.fdopen(os.open(path, flags | os.O_NOFOLLOW), mode)


def netbsd_makedev(major, minor):
    """NetBSD's dev_t: twelve bits of major from bit 8, and a minor split
    between bits 31-20 and 7-0.  (sys/sys/types.h, unchanged since 1.5.)

    For everything a Sun-2 /dev actually holds -- major well under 4096, minor
    under 256 -- this comes to (major << 8) | minor, which is how SunOS and
    Linux spell it too.  The full form is here so that a minor above 255 turns
    into the number the client expects rather than quietly into a small one.
    """
    return (((major << 8) & 0x000FFF00)
            | ((minor << 12) & 0xFFF00000)
            | (minor & 0x000000FF))


class SpecStat:
    """What os.lstat() would have said, if we could have made the node.

    A device node is a name and a pair of numbers for the client's own kernel
    to act on; nothing ever reads one over NFS.  mknod(2) is privileged and
    this server is not, so the tree holds an ordinary empty file where each
    node belongs and this stands in for its attributes.

    The times, link count and inode number are the placeholder's own, so the
    fileid stays unique and stable across a restart like every other file's.
    """

    def __init__(self, st, mode, uid, gid, rdev):
        self.st_mode = mode
        self.st_uid = uid
        self.st_gid = gid
        self.st_rdev = rdev
        self.st_size = 0
        self.st_blocks = 0
        self.st_nlink = 1
        self.st_dev = st.st_dev
        self.st_ino = st.st_ino
        self.st_atime = st.st_atime
        self.st_mtime = st.st_mtime
        self.st_ctime = st.st_ctime
        # --squash-to-root exists to paper over ownership we could not set on
        # disk.  There is none to paper over here: the specfile says who owns
        # the node, and that is the answer.
        self.from_spec = True


def load_devices(specfiles):
    """Read mtree(8) specfiles into {path: (mode, uid, gid, rdev)}.

    This is what NetBSD's own dev/MAKEDEV prints with -s, an option it has for
    exactly this reason -- building a /dev without being root.  Taking the
    numbers from it rather than writing them out here means they cannot be
    ours to get wrong, and a different release brings its own.

        ./console type=char device=netbsd,0,0 mode=600 gid=0 uid=0

    Paths are relative to the directory the specfile is in, so the file lives
    in the /dev it describes.  Only char and block entries are read: a
    directory in the list is an ordinary directory on disk.
    """
    kinds = {"char": stat.S_IFCHR, "block": stat.S_IFBLK}
    devices = {}
    for spec in specfiles:
        base = os.path.dirname(os.path.realpath(spec))
        with open(spec) as fh:
            for line in fh:
                fields = line.split()
                if not fields or fields[0].startswith("#"):
                    continue
                kw = dict(f.split("=", 1) for f in fields[1:] if "=" in f)
                ifmt = kinds.get(kw.get("type"))
                if ifmt is None or "device" not in kw:
                    continue
                numbers = kw["device"].split(",")
                # "netbsd,major,minor" names the encoding; some specfiles
                # leave it out and give the pair on its own.
                major, minor = (int(n, 0) for n in numbers[-2:])
                path = os.path.join(base, fields[0][2:] if fields[0].startswith("./")
                                    else fields[0])
                devices[canon(path)] = (
                    ifmt | (int(kw.get("mode", "600"), 8) & 0o7777),
                    int(kw.get("uid", 0)), int(kw.get("gid", 0)),
                    netbsd_makedev(major, minor))
    return devices


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
                 writable_trees=(), squash=False, tsize=DEFAULT_TSIZE,
                 devices=None):
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
        # Paths to report as device nodes rather than as the placeholder files
        # they are.  Empty for a client that brought its own /dev.
        self.devices = devices or {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))

    def log(self, *a):
        if self.debug:
            note(*a)

    def is_writable(self, path):
        real = os.path.realpath(path)
        if real in self.writable:
            return True
        for tree in self.writable_trees:
            if real == tree or real.startswith(tree + os.sep):
                return True
        return False

    def lstat(self, path):
        """os.lstat(), except where the tree stands in for something this
        server could not create.  Every attribute a client is told about goes
        through here."""
        st = os.lstat(path)
        if self.devices:
            spec = self.devices.get(canon(path))
            if spec is not None:
                return SpecStat(st, *spec)
        return st

    def attrs(self, st):
        return pack_fattr(st, self.squash and not getattr(st, "from_spec", False))

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
                self.log(f"error handling a call from {addr[0]}:", repr(exc))
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
            try:
                return self.nfs(xid, proc, d, addr)
            except Exception as exc:                      # never answer nothing
                # Every NFSv2 reply begins with a status, and a non-zero one
                # means nothing follows, so this is a well-formed answer to
                # any procedure.  A client that gets no answer at all has no
                # way to stop asking; one that gets an error can give up and
                # say so.
                self.log(f"{addr[0]} {NFS2_PROCS.get(proc, proc)}: "
                         f"{exc!r} -> IO")
                return self.accepted(xid, SUCCESS, struct.pack("!I", NFSERR_IO))
        if prog == MOUNTPROG:
            if vers not in MOUNT_VERSIONS:
                # The whole point of registering a version we do not serve:
                # this reply is what sends a version-3 client back down to a
                # version we do.  See --mount-versions.
                return self.accepted(xid, PROG_MISMATCH,
                                     struct.pack("!II", MOUNT_VERSIONS[0],
                                                 MOUNT_VERSIONS[-1]))
            return self.mount(xid, proc, d, addr)
        return self.accepted(xid, PROG_UNAVAIL)

    # ------------------------------------------------------------ MOUNT v1/v2
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
                    fd = os.open(target, os.O_CREAT | os.O_WRONLY
                                 | os.O_TRUNC | os.O_NOFOLLOW, perm)
                    if size not in (0, 0xFFFFFFFF):
                        os.ftruncate(fd, size)
                    os.close(fd)
                st = self.lstat(target)
            except OSError as exc:
                return err(errno_to_nfs(exc), f" {self.shortname(target)}")
            handle = self.export.remember(canon(target))
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
            self.export.by_handle.pop(Export.handle_for(canon(target)), None)
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
            self.export.renamed(src, dst)
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
            self.export.remember(canon(target))
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
                if size != 0xFFFFFFFF and stat.S_ISREG(self.lstat(path).st_mode):
                    with opennofollow(path, "r+b") as fh:
                        fh.truncate(size)
                    self.log(f"{who} SETATTR {self.shortname(path)} size={size} -> OK")
                elif size != 0xFFFFFFFF:
                    self.log(f"{who} SETATTR {self.shortname(path)} "
                             f"(size ignored: not a regular file) -> OK")
                else:
                    # mode, uid and gid we cannot honour as an ordinary user,
                    # and a swap file does not care.  Report success with the
                    # attributes unchanged rather than failing the boot.
                    self.log(f"{who} SETATTR {self.shortname(path)} "
                             f"(mode/uid/gid ignored) -> OK")
                st = self.lstat(path)
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
                # Inside the try: the name a handle was minted from can stop
                # existing between one call and the next, and an lstat that
                # raises out of here answers the client with nothing at all --
                # which costs it a retransmit every 64 seconds for ever.
                if not stat.S_ISREG(self.lstat(path).st_mode):
                    return err(NFSERR_IO, f" {self.shortname(path)} is not a regular file")
                with opennofollow(path, "r+b") as fh:
                    fh.seek(offset)
                    fh.write(data)
                st = self.lstat(path)
            except FileNotFoundError:
                # The handle resolved, but to a name that is gone.  STALE, not
                # NOENT: NOENT is about a name the client asked for, and the
                # client did not ask for one -- it gave us a handle.
                return err(NFSERR_STALE, f" {self.shortname(path)}")
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
                st = self.lstat(path)
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
                st = self.lstat(target)
            except OSError as exc:
                return err(errno_to_nfs(exc), f" {name} in {self.shortname(path)}")
            handle = self.export.remember(canon(target))
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
                return err(errno_to_nfs(exc), f" {self.shortname(path)}")
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
                st = self.lstat(path)
                if stat.S_ISDIR(st.st_mode):
                    return err(NFSERR_ISDIR)
                # A device node in the export is a name and a pair of numbers
                # for the client's own kernel to act on; opening it here would
                # open this host's device of the same numbers.  No client reads
                # one over NFS, and this server will not offer to.
                if not stat.S_ISREG(st.st_mode):
                    return err(NFSERR_IO, f" {self.shortname(path)} is not a regular file")
                with opennofollow(path, "rb") as fh:
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
                    # The placeholder's own inode, which is what self.lstat()
                    # would report for a device node too.
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
                         f"(default {DEFAULT_TSIZE}).  Advisory: a SunOS "
                         f"kernel mounting its root ignores it.")
    ap.add_argument("--squash-to-root", action="store_true",
                    help="report every file as owned by root.  A SunOS root is "
                         "root-owned throughout, but this server must own the "
                         "files on disk to be able to write them.")
    ap.add_argument("--devices", action="append", default=[],
                    metavar="SPECFILE",
                    help="report the paths listed in this mtree(8) specfile "
                         "as the device nodes they describe (repeatable).  "
                         "Written by NetBSD's own dev/MAKEDEV -s, so a root "
                         "filesystem can have a /dev without anyone being "
                         "root.  Paths in it are relative to the directory "
                         "the specfile is in.")
    ap.add_argument("--mount-versions", default="1,2",
                    metavar="LIST",
                    help="MOUNT versions to register with the portmapper "
                         "(default 1,2).  Versions 1 and 2 are served; any "
                         "other version listed is registered and then "
                         "answered with PROG_MISMATCH, which is what makes a "
                         "client that asks for MOUNT version 3 fall back to "
                         "one this server speaks instead of stopping.  Only "
                         "list 3 when unfs3 is not running: the portmapper "
                         "keeps the first registration it is given.")
    ap.add_argument("--no-register", action="store_true",
                    help="do not register with the portmapper")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    root = os.path.realpath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"{root} is not a directory")
    allow = {a for a in args.allow.split(",") if a}

    try:
        mount_versions = [int(v) for v in args.mount_versions.split(",") if v]
    except ValueError:
        raise SystemExit(f"--mount-versions: not a list of numbers: {args.mount_versions}")
    if not mount_versions:
        raise SystemExit("--mount-versions: at least one version is needed")

    devices = load_devices(args.devices)

    export = Export(root, debug=args.debug)
    server = Server(export, args.port, allow, args.debug, args.writable,
                    args.writable_tree, args.squash_to_root, args.tsize,
                    devices)
    # Rotation.  Rename the log aside and send SIGHUP; without this the daemon
    # writes into the renamed file for ever, since it holds the descriptor and
    # never looks at the name again.  start.sh runs us under nohup, which
    # leaves SIGHUP ignored -- installing a handler overrides that, and is in
    # any case better than being killed by a stray one.
    log_path = logfile()
    if log_path is not None:
        def rotate(_signum, _frame):
            reopen_log(log_path)
            note(f"reopened {log_path} on SIGHUP")
        signal.signal(signal.SIGHUP, rotate)
        note(f"logging to {log_path}; SIGHUP reopens it")

    for spec in args.devices:
        note(f"device nodes: {spec}")
    if devices:
        missing = [d for d in devices if not os.path.exists(d)]
        note(f"{len(devices)} device nodes reported from specfiles"
             + (f", {len(missing)} with no placeholder file" if missing else ""))
    for w in sorted(server.writable):
        note(f"writable file: {w}")
    for t in server.writable_trees:
        note(f"writable tree: {t}")
    if server.squash:
        note("reporting every file as owned by root")

    registered = [(NFSPROG, NFSVERS, "nfs v2")]
    registered += [(MOUNTPROG, v,
                    f"mount v{v}" + ("" if v in MOUNT_VERSIONS else " (answered PROG_MISMATCH)"))
                   for v in mount_versions]
    if not args.no_register:
        for prog, vers, what in registered:
            portmap(PMAPPROC_UNSET, prog, vers, 0)
            if not portmap(PMAPPROC_SET, prog, vers, args.port):
                raise SystemExit(f"the portmapper would not register {what}")
            note(f"registered {what} on udp/{args.port}")

    note(f"serving {root} over NFSv2 on udp/{args.port}, "
         f"advertising tsize {args.tsize}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if not args.no_register:
            for prog, vers, _ in registered:
                try:
                    portmap(PMAPPROC_UNSET, prog, vers, 0)
                except SystemExit:
                    pass


if __name__ == "__main__":
    main()
