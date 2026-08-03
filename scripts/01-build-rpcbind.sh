#!/bin/sh
# Build rpcbind from upstream source.
#
# Why not just use Debian's package?  Two reasons, both found the hard way:
#
#   * Debian's rpcbind refuses to start unless euid is 0 and takes a lock in
#     /run, so a CAP_NET_BIND_SERVICE capability is not enough to run it as an
#     ordinary user.  src/rpcbind-unprivileged.patch removes exactly those two
#     obstacles and nothing else.
#   * Debian additionally disables RPC indirect calls and hides them behind a
#     -r flag.  The Sun-3 finds bootparamd by sending PMAPPROC_CALLIT to the
#     broadcast address, so indirect calls are not optional here.  Upstream
#     enables them with ./configure --enable-rmtcalls.
#
# The alternative is to have root install and run the system rpcbind service
# instead; see the "Alternatives" section of README.md.  This way keeps the
# whole stack unprivileged apart from the one capability.

. "$(dirname -- "$0")/common.sh"

VERSION=${RPCBIND_VERSION:-1.2.6}
TARBALL=rpcbind-$VERSION.orig.tar.bz2
URL=http://deb.debian.org/debian/pool/main/r/rpcbind/rpcbind_$VERSION.orig.tar.bz2
PATCHFILE=$BOOTDIR/src/rpcbind-unprivileged.patch

command -v gcc >/dev/null || die "gcc is missing (apt install build-essential)"
[ -e /usr/include/tirpc/rpc/rpc.h ] || die "libtirpc-dev headers are missing"
[ -r "$PATCHFILE" ] || die "$PATCHFILE is missing"

mkdir -p "$DIST" "$SBIN" "$BOOTDIR/src" "$LOG"

if [ ! -f "$DIST/$TARBALL" ]; then
	say "downloading $TARBALL"
	curl -fsSL --retry 3 -o "$DIST/$TARBALL.part" "$URL"
	mv "$DIST/$TARBALL.part" "$DIST/$TARBALL"
fi

SRC=$BOOTDIR/src/rpcbind-$VERSION
rm -rf "$SRC"
say "unpacking into $SRC"
tar xjf "$DIST/$TARBALL" -C "$BOOTDIR/src"

say "applying rpcbind-unprivileged.patch"
( cd "$SRC" && patch -p1 -l --no-backup-if-mismatch <"$PATCHFILE" ) \
	|| die "patch did not apply -- upstream version changed?"
grep -q 'not superuser' "$SRC/src/rpcbind.c" \
	&& die "the euid check is still present; the patch applied to the wrong place"

say "configuring (--enable-rmtcalls is what makes PMAPPROC_CALLIT work)"
( cd "$SRC" && ./configure \
	--prefix="$BOOTDIR/local" \
	--enable-rmtcalls \
	--enable-debug \
	--with-statedir="$RUN" \
	--with-systemdsystemunitdir=no \
	CPPFLAGS="-DRPCBINDDLOCK='\"$RUN/rpcbind.lock\"'" \
	>"$LOG/rpcbind-configure.log" 2>&1 ) \
	|| { tail -30 "$LOG/rpcbind-configure.log" >&2; die "rpcbind configure failed"; }

say "compiling"
( cd "$SRC" && make -j"$(nproc)" >"$LOG/rpcbind-build.log" 2>&1 ) \
	|| { tail -30 "$LOG/rpcbind-build.log" >&2; die "rpcbind build failed"; }

for b in rpcbind rpcinfo; do
	src=$(find "$SRC" -name "$b" -type f -perm -u+x | head -1)
	[ -n "$src" ] || die "$b was not built"
	rm -f "$SBIN/$b"          # may be a stale symlink from an older layout
	cp "$src" "$SBIN/$b"
done

say "built:"
ls -l "$SBIN/rpcbind" "$SBIN/rpcinfo"
