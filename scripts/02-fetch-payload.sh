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
	SUN2_KERNEL=${NETBSD_KERNEL_SUN2:-netbsd-RAMDISK}
	say "a sun2 client is configured; fetching its ND boot programs"
	for f in bootyy netboot; do
		fetch "$SUN2_BASE/installation/netboot/$f" "$DIST/sun2-$f"
		cp -f "$DIST/sun2-$f" "$BOOTDIR/payload/sun2-$f"
	done

	# ...and its kernel, which is a different architecture from the sun3
	# one above and so a separate download and a separate MD5 file.
	fetch "$SUN2_BASE/binary/kernel/$SUN2_KERNEL.gz" "$DIST/sun2-$SUN2_KERNEL.gz"
	fetch "$SUN2_BASE/binary/kernel/MD5"             "$DIST/sun2-kernel-MD5"

	say "verifying sun2 $SUN2_KERNEL.gz against the upstream MD5 file"
	want=$(awk -v f="($SUN2_KERNEL.gz)" '$2 == f { print $4 }' "$DIST/sun2-kernel-MD5")
	[ -n "$want" ] || die "$SUN2_KERNEL.gz is not listed in the sun2 MD5 file -- check NETBSD_KERNEL_SUN2"
	got=$(md5sum <"$DIST/sun2-$SUN2_KERNEL.gz" | cut -d' ' -f1)
	[ "$want" = "$got" ] || die "MD5 mismatch for sun2 $SUN2_KERNEL.gz (want $want, got $got)"

	say "decompressing sun2 $SUN2_KERNEL"
	gzip -dc "$DIST/sun2-$SUN2_KERNEL.gz" >"$BOOTDIR/payload/sun2-$SUN2_KERNEL.new"
	mv "$BOOTDIR/payload/sun2-$SUN2_KERNEL.new" "$BOOTDIR/payload/sun2-$SUN2_KERNEL"

	SUN2_FILES="sun2-bootyy sun2-netboot sun2-$SUN2_KERNEL"
fi

# --- NetBSD 2.0 for sun2, with a real root ----------------------------------
# A different release from the one above, and old enough that it lives in the
# archive rather than on the mirrors: its own base URL, its own MD5 files, its
# own copies of bootyy and netboot.  Only fetched when the table asks for it.
#
# archive.netbsd.org puts a "trivial botcatcher" in front of the big files: a
# form asking what you are here for, whose answer is a key= parameter.  Say
# NetBSD, because that is what we are here for.
archive_fetch() {  # archive_fetch <url> <destination>
	[ -f "$2" ] && { say "have $(basename "$2")"; return 0; }
	say "downloading $(basename "$2")"
	curl -fsSL --retry 3 -o "$2.part" "$1?key=NetBSD"
	mv "$2.part" "$2"
}

NETBSD2_FILES=
if clients | awk '$5 == "netbsd2" { found = 1 } END { exit !found }'; then
	NB2_BASE=https://archive.netbsd.org/pub/NetBSD-archive/NetBSD-$NETBSD2_RELEASE/sun2
	NB2_KERNEL=$NETBSD2_KERNEL_SUN2
	say "a netbsd2 client is configured; fetching NetBSD $NETBSD2_RELEASE/sun2"

	for f in bootyy netboot; do
		archive_fetch "$NB2_BASE/installation/netboot/$f" "$DIST/netbsd2-$f"
		cp -f "$DIST/netbsd2-$f" "$BOOTDIR/payload/netbsd2-$f"
	done

	archive_fetch "$NB2_BASE/binary/kernel/$NB2_KERNEL.gz" "$DIST/netbsd2-$NB2_KERNEL.gz"
	archive_fetch "$NB2_BASE/binary/kernel/MD5"            "$DIST/netbsd2-kernel-MD5"
	say "verifying netbsd2 $NB2_KERNEL.gz against the upstream MD5 file"
	want=$(awk -v f="($NB2_KERNEL.gz)" '$2 == f { print $4 }' "$DIST/netbsd2-kernel-MD5")
	[ -n "$want" ] || die "$NB2_KERNEL.gz is not listed in the NetBSD $NETBSD2_RELEASE MD5 file -- check NETBSD2_KERNEL_SUN2"
	got=$(md5sum <"$DIST/netbsd2-$NB2_KERNEL.gz" | cut -d' ' -f1)
	[ "$want" = "$got" ] || die "MD5 mismatch for netbsd2 $NB2_KERNEL.gz (want $want, got $got)"

	say "decompressing netbsd2 $NB2_KERNEL"
	gzip -dc "$DIST/netbsd2-$NB2_KERNEL.gz" >"$BOOTDIR/payload/netbsd2-$NB2_KERNEL.new"
	mv "$BOOTDIR/payload/netbsd2-$NB2_KERNEL.new" "$BOOTDIR/payload/netbsd2-$NB2_KERNEL"

	# The distribution sets.  These are the root filesystem itself, not
	# something the PROM ever fetches, so they stay in dist/ and
	# scripts/04-make-netbsd2-root.sh unpacks them; base.tgz alone is 74MB.
	archive_fetch "$NB2_BASE/binary/sets/MD5" "$DIST/netbsd2-sets-MD5"
	for f in $NETBSD2_SETS; do
		archive_fetch "$NB2_BASE/binary/sets/$f.tgz" "$DIST/netbsd2-$f.tgz"
		want=$(awk -v f="($f.tgz)" '$2 == f { print $4 }' "$DIST/netbsd2-sets-MD5")
		[ -n "$want" ] || die "$f.tgz is not listed in the NetBSD $NETBSD2_RELEASE sets MD5 file -- check NETBSD2_SETS"
		got=$(md5sum <"$DIST/netbsd2-$f.tgz" | cut -d' ' -f1)
		[ "$want" = "$got" ] || die "MD5 mismatch for netbsd2 $f.tgz (want $want, got $got)"
		say "verified $f.tgz"
	done

	NETBSD2_FILES="netbsd2-bootyy netbsd2-netboot netbsd2-$NB2_KERNEL"
fi

# --- SunOS 4.0.3 for sun2 ---------------------------------------------------
# Not a download: these come off a local tape extraction, and that directory is
# read-only as far as we are concerned.  Copy them in under sunos- names and
# check them against the hashes recorded beside them.
SUNOS_FILES=
if clients | awk '$5 == "sunos" { found = 1 } END { exit !found }'; then
	src=${SUNOS_NETBOOT_DIR:-../Sun-2_DiskImage/netboot}
	case $src in /*) ;; *) src=$BOOTDIR/$src ;; esac
	[ -d "$src" ] || die "SUNOS_NETBOOT_DIR ($src) does not exist"
	say "a sunos client is configured; taking its boot programs from ${src}"
	( cd "$src" && sha256sum -c --quiet SHA256SUMS ) \
		|| die "$src does not match its own SHA256SUMS"
	for f in sun2.bb boot.sun2 vmunix; do
		[ -f "$src/$f" ] || die "$src/$f is missing"
		cp -f "$src/$f" "$BOOTDIR/payload/sunos-$f"
	done
	SUNOS_FILES='sunos-sun2.bb sunos-boot.sun2 sunos-vmunix'
fi

# --- optional miniroot ------------------------------------------------------
if [ "${NETBSD_FETCH_MINIROOT:-no}" = yes ]; then
	fetch "$BASE/installation/miniroot/miniroot.fs.gz" "$DIST/miniroot.fs.gz"
	gzip -dc "$DIST/miniroot.fs.gz" >"$BOOTDIR/payload/miniroot.fs.new"
	mv "$BOOTDIR/payload/miniroot.fs.new" "$BOOTDIR/payload/miniroot.fs"
fi

# --- local manifest ---------------------------------------------------------
# Everything this run put in payload/, in the order it was fetched.
# shellcheck disable=SC2086
ALL_FILES="netboot $KERNEL $SUN2_FILES $NETBSD2_FILES $SUNOS_FILES"
MANIFEST=$BOOTDIR/payload/SHA256SUMS
if [ -f "$MANIFEST" ]; then
	say "verifying against payload/SHA256SUMS"
	( cd "$BOOTDIR/payload" && sha256sum -c --ignore-missing SHA256SUMS ) \
		|| die "payload changed unexpectedly; delete payload/SHA256SUMS if that was intentional"
	# Adding a client can add a file the manifest predates.  Record those
	# rather than leaving them the only unchecked things in the directory.
	for f in $ALL_FILES; do
		grep -q "  $f\$" "$MANIFEST" && continue
		( cd "$BOOTDIR/payload" && sha256sum "$f" >>SHA256SUMS )
		say "added $f to payload/SHA256SUMS"
	done
else
	( cd "$BOOTDIR/payload" && sha256sum $ALL_FILES >SHA256SUMS )
	say "recorded payload/SHA256SUMS"
fi

say "payload ready:"
ls -l "$BOOTDIR/payload"
