#!/bin/sh
# Build ndbootd, the Sun Network Disk server, for Linux.
#
# A Sun-2 cannot TFTP and does not do RARP: it loads its bootstrap over ND and
# learns its own IP address from the first ND reply.  ndbootd is NetBSD's
# server for that, and it is not packaged for Debian, so we build it here --
# with src/ndbootd-packet.c, an AF_PACKET replacement for its BPF-only raw
# interface, and src/ndbootd-boot1-dir.patch, which lets each client have its
# own first-stage boot program.  See README, "A Sun-2".
#
# Nothing here needs privilege.  The finished binary needs cap_net_raw, which
# root/grant-privileges.sh grants.

. "$(dirname -- "$0")/common.sh"

# Pinned so this is reproducible: ndbootd is not released as a tarball, it
# lives in the NetBSD source tree, and trunk moves.
COMMIT=1029c5e85377626ba120ff6b82e5fc3f12a5379d
BASE=https://raw.githubusercontent.com/NetBSD/src/$COMMIT/usr.sbin/ndbootd
SRC=$BOOTDIR/src/ndbootd
VERSION=0.5

# sha256 of each upstream file at that commit.
SUMS='c09f13c0fc44f006843419a917b013aae88e40b104b6e1b66e51142e7552f254  ndbootd.c
f870b376930d1c46d0956dcc843a1638a25a0735f5161d720d0b38beb710bc66  ndbootd.h
4ecdb40da04afa3eebe8e75ce2bad60fdd911d4b28c3185130e7d21e0d8cc4a7  ndbootd.8
89fb4bd77b6605241e840b746fabd5196b9956dfff6f0924ea257c66d3cbaaa1  COPYING'

mkdir -p "$DIST" "$SRC" "$SBIN"

say "fetching ndbootd $VERSION from NetBSD $(echo "$COMMIT" | cut -c1-12)"
for f in ndbootd.c ndbootd.h ndbootd.8 COPYING; do
	cached=$DIST/ndbootd-$COMMIT-$f
	if [ ! -f "$cached" ]; then
		curl -fL --no-progress-meter --retry 3 -o "$cached.tmp" "$BASE/$f" \
			|| die "could not download $f"
		mv "$cached.tmp" "$cached"
	fi
	cp "$cached" "$SRC/$f"
done

# Verify before doing anything with them: this is the one part of the build
# that comes off the network.
( cd "$SRC" && printf '%s\n' "$SUMS" | sha256sum -c --quiet ) \
	|| die "ndbootd source does not match the recorded checksums"
say "checksums match"

# Our pieces.  Kept in src/ rather than generated so they are reviewable and
# tracked; the build only ever copies them in.
cp "$BOOTDIR/src/ndbootd-config.h"  "$SRC/config.h"
cp "$BOOTDIR/src/ndbootd-packet.c"  "$SRC/ndbootd-packet.c"

for p in ndbootd-linux ndbootd-boot1-dir; do
	say "applying src/$p.patch"
	( cd "$SRC" && patch -p0 --forward --silent < "$BOOTDIR/src/$p.patch" ) \
		|| die "src/$p.patch did not apply"
done

# -D__RCSID: ndbootd.c uses it before it includes config.h, so it cannot be
#            dealt with in the patch without touching that line too.
# -DNDBOOTD_PID_FILE: the default is /var/run, which we cannot write.  The
#            patch makes it overridable; start.sh manages the pidfile anyway,
#            but this keeps the daemon from trying somewhere it has no business.
say "compiling"
( cd "$SRC" && gcc -O2 -Wall -o ndbootd ndbootd.c \
	-DHAVE_CONFIG_H \
	-D'__RCSID(x)=' \
	-DNDBOOTD_PID_FILE="\"$RUN/ndbootd.internal.pid\"" \
	-I. ) || die "ndbootd did not compile"

# Capabilities live on the inode, so replacing the file loses them.
had_cap=$(getcap "$SBIN/ndbootd" 2>/dev/null || true)
rm -f "$SBIN/ndbootd"
cp "$SRC/ndbootd" "$SBIN/ndbootd"

say "built sbin/ndbootd"
if [ -n "$had_cap" ]; then
	warn "sbin/ndbootd was replaced, so its capability is gone:"
	warn "    $had_cap"
	warn "  an administrator must re-run root/grant-privileges.sh"
fi
