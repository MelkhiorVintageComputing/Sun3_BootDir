#!/bin/sh
# Build atftpd from upstream source with src/atftpd-broadcast.patch applied.
#
# Debian's binary cannot serve a Sun-3.  The PROM broadcasts its TFTP read
# request, and atftpd binds the data socket to the address that request was
# sent to -- 255.255.255.255 -- so the client's ACKs are never delivered and
# the transfer dies after one block.  Read src/atftpd-broadcast.patch; it is
# nine lines and it explains the whole thing.
#
# tftpd-hpa is not an escape: it takes the destination address from IP_PKTINFO
# the same way and has no special case for a broadcast destination either.
#
# The daemon is started with --listen-local, which is what activates the fix.

. "$(dirname -- "$0")/common.sh"

VERSION=${ATFTP_VERSION:-0.8.0}
TARBALL=atftp-$VERSION.tar.gz
URL=http://deb.debian.org/debian/pool/main/a/atftp/atftp_$VERSION.orig.tar.gz
PATCHFILE=$BOOTDIR/src/atftpd-broadcast.patch

for t in gcc make autoconf automake; do
	command -v "$t" >/dev/null \
		|| die "$t is missing (apt install build-essential autoconf automake)"
done
[ -r "$PATCHFILE" ] || die "$PATCHFILE is missing"

mkdir -p "$DIST" "$SBIN" "$BOOTDIR/src" "$LOG"

if [ ! -f "$DIST/$TARBALL" ]; then
	say "downloading $TARBALL"
	curl -fsSL --retry 3 -o "$DIST/$TARBALL.part" "$URL"
	mv "$DIST/$TARBALL.part" "$DIST/$TARBALL"
fi

SRC=$BOOTDIR/src/atftp-$VERSION
rm -rf "$SRC"
say "unpacking into $SRC"
tar xzf "$DIST/$TARBALL" -C "$BOOTDIR/src"

say "applying atftpd-broadcast.patch"
( cd "$SRC" && patch -p1 -l --no-backup-if-mismatch <"$PATCHFILE" ) \
	|| die "patch did not apply -- upstream version changed?"
grep -q 'Bind to the wildcard address' "$SRC/tftpd.c" \
	|| die "patch applied somewhere unexpected"

# The Debian orig tarball ships no configure script.
say "running autoreconf"
( cd "$SRC" && autoreconf -fi >"$LOG/atftp-autoreconf.log" 2>&1 ) \
	|| { tail -20 "$LOG/atftp-autoreconf.log" >&2; die "autoreconf failed"; }

# Only the daemon is wanted, and none of the optional libraries have -dev
# packages installed here.
say "configuring"
( cd "$SRC" && ./configure --prefix="$BOOTDIR/local" \
	--disable-libreadline --disable-libwrap --disable-libpcre --disable-mtftp \
	>"$LOG/atftp-configure.log" 2>&1 ) \
	|| { tail -20 "$LOG/atftp-configure.log" >&2; die "atftp configure failed"; }

say "compiling"
( cd "$SRC" && make -j"$(nproc)" >"$LOG/atftp-build.log" 2>&1 ) \
	|| { tail -20 "$LOG/atftp-build.log" >&2; die "atftp build failed"; }

[ -x "$SRC/atftpd" ] || die "atftpd was not produced"
had_cap=$(getcap "$SBIN/atftpd" 2>/dev/null || true)
rm -f "$SBIN/atftpd"
cp "$SRC/atftpd" "$SBIN/atftpd"

say "built:"
ls -l "$SBIN/atftpd"

if [ -n "$had_cap" ]; then
	cat <<-EOF

	NOTE: sbin/atftpd was replaced, so its capability is gone -- capabilities
	      live on the file, not the path.  TFTP will not start until an
	      administrator re-runs root/grant-privileges.sh.
	EOF
fi
