#!/bin/sh
# Fetch the boot daemons from Debian without installing anything.
#
# "apt-get download" and "dpkg-deb -x" both work as an ordinary user, so no
# part of this needs root.  Every shared library these packages need is already
# present on a normal Debian 12 system; --check below verifies that rather than
# assuming it.
#
# Binaries land in pkg/ and are symlinked into sbin/.  The three that need a
# capability are *copied* instead: file capabilities live on the inode, and we
# want them on files we own inside this directory, not on anything under /usr.

. "$(dirname -- "$0")/common.sh"

# rpcbind and atftpd are deliberately absent -- Debian's builds cannot serve a
# Sun-3 (see scripts/01-build-rpcbind.sh and scripts/01-build-atftpd.sh, which
# build patched versions from upstream source instead).
PACKAGES='rarpd bootparamd tftp-hpa'
SUITE=${SUITE:-bookworm}

mkdir -p "$DIST" "$PKG" "$SBIN"

say "downloading .debs for: $PACKAGES"
( cd "$DIST" && for p in $PACKAGES; do
	# Pin the suite: this host has buster/bullseye/bookworm/sid all in
	# sources.list, and we want one predictable set.
	apt-get download "$p/$SUITE" 2>/dev/null || apt-get download "$p"
done )

say "extracting into $PKG"
rm -rf "$PKG"
mkdir -p "$PKG"
for p in $PACKAGES; do
	deb=$(ls -1t "$DIST"/"${p}"_*.deb 2>/dev/null | head -1) \
		|| die "no .deb downloaded for $p"
	[ -n "$deb" ] || die "no .deb downloaded for $p"
	dpkg-deb -x "$deb" "$PKG"
	printf '    %s\n' "${deb##*/}"
done

# --- wire up sbin/ ---------------------------------------------------------
#
# Copy (a capability is granted to this one by root/grant-privileges.sh):
#   rarpd    needs CAP_NET_RAW            (PF_PACKET socket)
# Symlinks (no privilege at all):
#   rpc.bootparamd, tftp
say "populating $SBIN"
rm -f "$SBIN"/rarpd "$SBIN"/rpc.bootparamd "$SBIN"/tftp
for b in rarpd; do
	src=$(findbin "$b") || die "$b not found in extracted packages"
	rm -f "$SBIN/$b"
	cp "$src" "$SBIN/$b"
done
for b in rpc.bootparamd tftp; do
	src=$(findbin "$b") || die "$b not found in extracted packages"
	ln -sf "$src" "$SBIN/$b"
done

# --- check the runtime dependencies are actually satisfiable ---------------
say "checking shared library dependencies"
missing=0
for b in "$SBIN"/rarpd "$SBIN"/rpc.bootparamd; do
	if ldd "$b" 2>/dev/null | grep -q 'not found'; then
		warn "$b has unresolved libraries:"
		ldd "$b" | grep 'not found' >&2
		missing=1
	fi
done
[ "$missing" -eq 0 ] || die "install the missing libraries, or extract those .debs here too"

say "done.  sbin/ now holds:"
ls -l "$SBIN"
