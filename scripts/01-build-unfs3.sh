#!/bin/sh
# Build unfs3, a user-space NFS server.
#
# Debian dropped the unfs3 package, and the in-kernel nfsd needs root, so we
# build upstream here.  unfsd is the one daemon in the stack that needs no
# privilege whatsoever: NFS lives on port 2049, which is already unprivileged.
#
# unfs3 speaks NFSv3 only.  That is fine for NetBSD/sun3, whose bootloader
# tries MOUNT v3 before falling back to v1 (sys/lib/libsa/nfs.c).  It is *not*
# enough for a SunOS 4.x boot program -- see README.md.

. "$(dirname -- "$0")/common.sh"

VERSION=${UNFS3_VERSION:-0.11.0}
TARBALL=unfs3-$VERSION.tar.gz
URL=https://github.com/unfs3/unfs3/releases/download/unfs3-$VERSION/$TARBALL

for t in gcc make flex bison rpcgen; do
	command -v "$t" >/dev/null \
		|| die "$t is missing (apt install build-essential flex bison rpcsvc-proto)"
done
[ -e /usr/include/tirpc/rpc/rpc.h ] \
	|| warn "libtirpc-dev headers not found; configure may fail"

mkdir -p "$DIST" "$SBIN" "$BOOTDIR/src"

if [ ! -f "$DIST/$TARBALL" ]; then
	say "downloading $TARBALL"
	curl -fsSL --retry 3 -o "$DIST/$TARBALL.part" "$URL"
	mv "$DIST/$TARBALL.part" "$DIST/$TARBALL"
fi

SRC=$BOOTDIR/src/unfs3-$VERSION
rm -rf "$SRC"
say "unpacking into $SRC"
tar xzf "$DIST/$TARBALL" -C "$BOOTDIR/src"

say "configuring"
( cd "$SRC" && ./configure --prefix="$BOOTDIR/local" >"$LOG/unfs3-configure.log" 2>&1 ) \
	|| { tail -30 "$LOG/unfs3-configure.log" >&2; die "unfs3 configure failed (see log/unfs3-configure.log)"; }

say "compiling"
( cd "$SRC" && make -j"$(nproc)" >"$LOG/unfs3-build.log" 2>&1 ) \
	|| { tail -30 "$LOG/unfs3-build.log" >&2; die "unfs3 build failed (see log/unfs3-build.log)"; }

[ -x "$SRC/unfsd" ] || die "unfsd binary not produced"
cp -f "$SRC/unfsd" "$SBIN/unfsd"

say "built $("$SBIN/unfsd" -h 2>&1 | head -1)"
ls -l "$SBIN/unfsd"
