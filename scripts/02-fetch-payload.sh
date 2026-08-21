#!/bin/sh
# Fetch what the Sun-3 will actually run.
#
#   payload/netboot   the second-stage bootstrap the PROM downloads over TFTP.
#                     It re-RARPs, asks bootparamd where its root is, then
#                     loads the kernel over NFS.
#   payload/<kernel>  the kernel netboot loads.
#
# With PAYLOAD=custom this script only checks that the files you named in
# config/sun3boot.conf exist.

. "$(dirname -- "$0")/common.sh"

mkdir -p "$BOOTDIR/payload"

if [ "$PAYLOAD" = custom ]; then
	: "${CUSTOM_NETBOOT:?set CUSTOM_NETBOOT in config/sun3boot.conf}"
	: "${CUSTOM_KERNEL:?set CUSTOM_KERNEL in config/sun3boot.conf}"
	for f in "$CUSTOM_NETBOOT" "$CUSTOM_KERNEL"; do
		[ -f "$BOOTDIR/$f" ] || die "$f does not exist -- put your own files there"
		printf '    %-40s %s bytes\n' "$f" "$(wc -c <"$BOOTDIR/$f")"
	done
	say "custom payload present; nothing to download"
	exit 0
fi

[ "$PAYLOAD" = netbsd ] || die "PAYLOAD must be 'netbsd' or 'custom', not '$PAYLOAD'"

BASE=https://cdn.netbsd.org/pub/NetBSD/NetBSD-$NETBSD_RELEASE/sun3
KERNEL=$NETBSD_KERNEL

# The sun3x kernels carry a 3X suffix; catch the easy mistake early.  Only one
# kernel is fetched, so a table mixing sun3 and sun3x clients cannot be served
# by it -- say that plainly rather than half-warning about the suffix.
ARCHES=$(clients | awk '{print $4}' | sort -u | tr '\n' ' ')
case "$ARCHES" in
'sun3 sun3x ') warn "CLIENTS mixes sun3 and sun3x but only $KERNEL is fetched; the other architecture will load a kernel it cannot run" ;;
'sun3x ')      case $KERNEL in *3X) ;; *) warn "every client is sun3x but NETBSD_KERNEL=$KERNEL -- you probably want ${KERNEL}3X" ;; esac ;;
'sun3 ')       case $KERNEL in *3X) warn "every client is sun3 but NETBSD_KERNEL=$KERNEL -- the 3X kernels are for sun3x" ;; esac ;;
esac

fetch() {  # fetch <url> <destination>
	[ -f "$2" ] && { say "have $(basename "$2")"; return 0; }
	say "downloading $(basename "$2")"
	curl -fsSL --retry 3 -o "$2.part" "$1"
	mv "$2.part" "$2"
}

# --- second-stage bootstrap -------------------------------------------------
# Not checksummed upstream, so we record a hash on first fetch and verify it on
# every later run.
fetch "$BASE/installation/netboot/netboot" "$DIST/netboot"
cp -f "$DIST/netboot" "$BOOTDIR/payload/netboot"

# --- kernel -----------------------------------------------------------------
fetch "$BASE/binary/kernel/$KERNEL.gz" "$DIST/$KERNEL.gz"
fetch "$BASE/binary/kernel/MD5"        "$DIST/kernel-MD5"

say "verifying $KERNEL.gz against the upstream MD5 file"
want=$(awk -v f="($KERNEL.gz)" '$2 == f { print $4 }' "$DIST/kernel-MD5")
[ -n "$want" ] || die "$KERNEL.gz is not listed in the upstream MD5 file -- check NETBSD_KERNEL"
got=$(md5sum <"$DIST/$KERNEL.gz" | cut -d' ' -f1)
[ "$want" = "$got" ] || die "MD5 mismatch for $KERNEL.gz (want $want, got $got)"

say "decompressing $KERNEL"
gzip -dc "$DIST/$KERNEL.gz" >"$BOOTDIR/payload/$KERNEL.new"
mv "$BOOTDIR/payload/$KERNEL.new" "$BOOTDIR/payload/$KERNEL"

# --- sun2 boot programs -----------------------------------------------------
# A Sun-2 loads these over ND, not TFTP, so they are only worth fetching when
# the table has a sun2 in it.  bootyy is the first stage that lives in blocks
# 1-15 of the disk ndbootd exports; netboot is the second stage, from block 16.
# Both are already raw binaries, as ndbootd requires.
SUN2_FILES=
if clients | awk '$4 == "sun2" { found = 1 } END { exit !found }'; then
	SUN2_BASE=https://cdn.netbsd.org/pub/NetBSD/NetBSD-$NETBSD_RELEASE/sun2
	say "a sun2 client is configured; fetching its ND boot programs"
	for f in bootyy netboot; do
		fetch "$SUN2_BASE/installation/netboot/$f" "$DIST/sun2-$f"
		cp -f "$DIST/sun2-$f" "$BOOTDIR/payload/sun2-$f"
	done
	SUN2_FILES='sun2-bootyy sun2-netboot'
fi

# --- optional miniroot ------------------------------------------------------
if [ "${NETBSD_FETCH_MINIROOT:-no}" = yes ]; then
	fetch "$BASE/installation/miniroot/miniroot.fs.gz" "$DIST/miniroot.fs.gz"
	gzip -dc "$DIST/miniroot.fs.gz" >"$BOOTDIR/payload/miniroot.fs.new"
	mv "$BOOTDIR/payload/miniroot.fs.new" "$BOOTDIR/payload/miniroot.fs"
fi

# --- local manifest ---------------------------------------------------------
MANIFEST=$BOOTDIR/payload/SHA256SUMS
if [ -f "$MANIFEST" ]; then
	say "verifying against payload/SHA256SUMS"
	( cd "$BOOTDIR/payload" && sha256sum -c --ignore-missing SHA256SUMS ) \
		|| die "payload changed unexpectedly; delete payload/SHA256SUMS if that was intentional"
else
	# shellcheck disable=SC2086
	( cd "$BOOTDIR/payload" && sha256sum netboot "$KERNEL" $SUN2_FILES >SHA256SUMS )
	say "recorded payload/SHA256SUMS"
fi

say "payload ready:"
ls -l "$BOOTDIR/payload"
