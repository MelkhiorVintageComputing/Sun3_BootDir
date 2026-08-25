# Sourced by every script in this directory.  Not executable on its own.

set -eu

BOOTDIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export BOOTDIR

CONF=$BOOTDIR/config/sun3boot.conf
[ -r "$CONF" ] || { echo "missing $CONF" >&2; exit 1; }
# shellcheck source=/dev/null
. "$CONF"

: "${SERVER_IF:?}" "${SERVER_IP:?}" "${PAYLOAD:?}"

# A config written before the table existed still names one client the old way.
if [ -z "${CLIENTS:-}" ]; then
	: "${CLIENT_NAME:?}" "${CLIENT_MAC:?}" "${CLIENT_IP:?}"
	CLIENTS="$CLIENT_NAME	$CLIENT_MAC	$CLIENT_IP	${CLIENT_ARCH:-sun3}"
fi

# 2049 belongs to whichever server the clients that do not ask for it expect
# to find there; see the config.
NFS2D_PORT=${NFS2D_PORT:-2049}
UNFSD_PORT=${UNFSD_PORT:-2050}
NFS2D_TSIZE=${NFS2D_TSIZE:-1024}

# payload=netbsd2: a release of its own, old enough to be in the archive only.
NETBSD2_RELEASE=${NETBSD2_RELEASE:-2.0}
NETBSD2_KERNEL_SUN2=${NETBSD2_KERNEL_SUN2:-netbsd-DISKLESS}
NETBSD2_SETS=${NETBSD2_SETS:-'base etc'}

DIST=$BOOTDIR/dist
PKG=$BOOTDIR/pkg
SBIN=$BOOTDIR/sbin
ETC=$BOOTDIR/etc
TFTPBOOT=$BOOTDIR/tftpboot
# The ND equivalent: first-stage boot programs, one per client, under the same
# per-client name.  Not tftpboot/ -- that already holds the second stage under
# that very name, and atftpd has no business serving these.
NDBOOT=$BOOTDIR/ndboot
NFSROOT=$BOOTDIR/nfsroot
RUN=$BOOTDIR/run
LOG=$BOOTDIR/log

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# 192.168.0.40 -> C0A80028, the filename the Sun-3 PROM asks TFTP for.
# The octet variables are underscored because /bin/sh has no locals and the
# callers loop over client fields with short names of their own.
hexip() {
	IFS='.' read -r _o1 _o2 _o3 _o4 <<-EOF
	$1
	EOF
	[ -n "${_o4:-}" ] || die "not a dotted-quad IPv4 address: $1"
	printf '%02X%02X%02X%02X' "$_o1" "$_o2" "$_o3" "$_o4"
}

# The name the PROM actually requests.  sun3 asks for the bare hex string;
# sun3x appends its architecture.  (NetBSD sun3/INSTALL.txt, "Boot/Install
# from NFS server".)
# A sun2 name is not for TFTP and not for rarpd: a Sun-2 PROM does no RARP at
# all and loads its bootstrap over ND.  The name is the one ndbootd would use
# when serving second-stage programs out of a directory, which is the same
# convention a Sun-3 TFTPs by.  (ndbootd(8), and README, "A Sun-2".)
tftpname() {  # tftpname <ip> [arch]
	case ${2:-sun3} in
	sun3)  hexip "$1" ;;
	sun3x) printf '%s.SUN3X' "$(hexip "$1")" ;;
	sun2)  printf '%s.SUN2' "$(hexip "$1")" ;;
	*)     die "client arch must be sun2, sun3 or sun3x, not '${2:-}'" ;;
	esac
}

# The client table, one "<name> <MAC> <IP> <arch> <payload>" per line, comments
# and blank lines removed and the optional fields defaulted.  Every generator
# loops over this, so read five fields even where you only want four.
clients() {
	printf '%s\n' "$CLIENTS" | sed 's/#.*//' \
		| awk 'NF { print $1, $2, $3, (NF >= 4 ? $4 : "sun3"), (NF >= 5 ? $5 : "netbsd") }'
}

# Reject a table that would produce configuration nobody can debug: rarpd would
# answer whichever duplicate came first, and two clients sharing a name would
# share a bootparams entry and an NFS root.
check_clients() {
	[ -n "$(clients)" ] || die "CLIENTS is empty -- nothing to configure"
	for field in 1:name 2:MAC 3:IP; do
		dup=$(clients | awk -v f="${field%%:*}" '{print $f}' | sort | uniq -d)
		[ -z "$dup" ] || die "duplicate client ${field#*:} in CLIENTS: $dup"
	done
	clients | while read -r n m i a p; do
		case $m in
		[0-9a-fA-F]*:*:*:*:*:*) ;;
		*) die "client $n: '$m' is not an Ethernet address" ;;
		esac
		hexip "$i" >/dev/null
		case $a in sun2|sun3|sun3x) ;; *) die "client $n: arch must be sun2, sun3 or sun3x, not '$a'" ;; esac
		case $p in
		netbsd) ;;
		sunos)
			# Only the sun2 SunOS boot programs are on hand.  A sun3
			# SunOS boot would want its own boot.sun3 and vmunix.
			[ "$a" = sun2 ] || die "client $n: payload sunos is only set up for sun2, not $a" ;;
		netbsd2)
			# Same again: the archive release is fetched for sun2
			# only, because that is the machine it is old enough for.
			[ "$a" = sun2 ] || die "client $n: payload netbsd2 is only set up for sun2, not $a" ;;
		*) die "client $n: payload must be netbsd, netbsd2 or sunos, not '$p'" ;;
		esac
	done || exit 1
}

# A NetBSD 2.0 kernel asks the portmapper for MOUNT version 3, then 2, then 1,
# and stops at the first version that answers at all -- so if unfs3 is there on
# version 3 it mounts its root over NFSv3, and a netbsd2 root cannot be served
# that way: its /dev is 855 placeholder files that only nfs2d knows are device
# nodes.  So when such a client is configured, nfs2d takes every MOUNT version
# (answering 3 with PROG_MISMATCH, which is what sends the kernel back down to
# 2) and unfs3 stands down.  Nothing is lost: a NetBSD bootloader falls back to
# MOUNT version 1 by itself, which is how the Sun-3s keep booting.
# See README, "Which server answers MOUNT".
nfs2d_answers_every_version() {
	[ -n "$(clients | awk '$5 == "netbsd2"')" ]
}

# The per-client arguments tools/nfs2d.py needs: which trees it may write,
# which specfiles describe a /dev it could not create, and whether it has to
# answer every MOUNT version.  Worked out here so that start.sh and dryrun.sh
# cannot drift apart about what the server is being asked to do.  Arguments go
# to stdout, the running commentary to stderr.
nfs2d_args() {
	_squash=
	for _n in $(clients | awk '$5 == "sunos" || $5 == "netbsd2" { print $1 }'); do
		case $(clients | awk -v n="$_n" '$1 == n { print $5 }') in
		netbsd2)
			# A whole root filesystem, and one that needs a /dev this
			# server could not create: mknod(2) is privileged, so the
			# tree holds a placeholder file per node and the specfile
			# NetBSD's own MAKEDEV -s wrote says what each one is.
			printf ' --writable-tree %s' "$NFSROOT/$_n"
			_squash=--squash-to-root
			if [ -f "$NFSROOT/$_n/dev/MAKEDEV.spec" ]; then
				printf ' --devices %s' "$NFSROOT/$_n/dev/MAKEDEV.spec"
				say "$_n has a NetBSD root filesystem: read-write, /dev from its specfile" >&2
			else
				warn "$NFSROOT/$_n has no dev/MAKEDEV.spec, so it has no /dev"
				warn "  run scripts/04-make-netbsd2-root.sh $_n"
			fi
			;;
		*)
			# A sunos client swaps over NFS, so its swap file -- and
			# nothing else in the export -- has to be writable, until
			# it has a root filesystem of its own.  That tree is owned
			# by us rather than by root, so nfs2d is told to report
			# root ownership, which is what SunOS expects.
			if [ -f "$NFSROOT/$_n/etc/rc.boot" ]; then
				printf ' --writable-tree %s' "$NFSROOT/$_n"
				_squash=--squash-to-root
				say "$_n has a SunOS root filesystem: serving it read-write" >&2
			else
				printf ' --writable %s' "$NFSROOT/$_n/swap"
			fi
			;;
		esac
	done
	[ -z "$_squash" ] || printf ' %s' "$_squash"
	# See start.sh, nfs2d_answers_every_version, and README, "Which server
	# answers MOUNT".
	[ -z "$(clients | awk '$5 == "netbsd2"')" ] || printf ' --mount-versions 1,2,3'
}

# The whole boot chain after RARP is ordinary IP: TFTP, bootparams and NFS
# replies are routed, not sent back down the wire the request arrived on.  A
# client the host would answer via a gateway therefore RARPs fine and then goes
# silent, which is a confusing way to fail.  Say so instead.

# The interface a reply to <ip> would leave by, empty if it would go to a
# gateway.  Clients can sit on different interfaces -- real hardware on the LAN
# port, an emulator on a bridge -- so this answers per address, not globally.
route_dev() {  # route_dev <ip>
	_r=$(ip -4 route get "$1" 2>/dev/null | head -1) || return 1
	case $_r in *" via "*) return 1 ;; esac
	case $_r in *" dev "*) ;; *) return 1 ;; esac
	_r=${_r#*" dev "}
	printf '%s' "${_r%% *}"
}

# Administratively up, i.e. IFF_UP.  This is what decides whether rarpd
# enumerates an interface at startup -- carrier does not come into it, and a
# bridge over an emulator's tap has no carrier until the emulator runs.
iface_up() {  # iface_up <interface>
	[ -r "/sys/class/net/$1/flags" ] || return 1
	[ "$(( $(cat "/sys/class/net/$1/flags") & 1 ))" -eq 1 ]
}

# Whether anything is actually attached.  Informational: no carrier on a
# bridge just means the emulator has not started yet.
iface_carrier() {  # iface_carrier <interface>
	[ "$(cat "/sys/class/net/$1/carrier" 2>/dev/null)" = 1 ]
}

# SunOS's /boot has no netmask, so it broadcasts its bootparams request to the
# 4.2BSD all-zeros address -- the network address, not 255.255.255.255.  Linux
# installs a broadcast route for the all-ones form only, so that datagram is
# dropped in the input path and no daemon sees it at all.
# root/allow-oldstyle-broadcast.sh adds the address; this is how the checks
# notice it is missing.
netaddr() {  # netaddr <ip> <prefixlen>
	IFS='.' read -r _b1 _b2 _b3 _b4 <<-EOF
	$1
	EOF
	_bip=$(( (_b1 << 24) | (_b2 << 16) | (_b3 << 8) | _b4 ))
	if [ "$2" -ge 32 ]; then
		_bmask=4294967295
	else
		_bmask=$(( (4294967295 << (32 - $2)) & 4294967295 ))
	fi
	_bnet=$(( _bip & _bmask ))
	printf '%d.%d.%d.%d' $(( (_bnet >> 24) & 255 )) $(( (_bnet >> 16) & 255 )) \
		$(( (_bnet >> 8) & 255 )) $(( _bnet & 255 ))
}

iface_prefix() {  # iface_prefix <interface>
	ip -o -4 addr show dev "$1" 2>/dev/null \
		| awk '{ split($4, a, "/"); if (a[2] != "" && a[2] != 32) { print a[2]; exit } }'
}

# Prints the all-zeros broadcast address a SunOS client would use.
oldstyle_bcast_addr() {  # oldstyle_bcast_addr <client-ip>
	_bdev=$(route_dev "$1") || return 1
	_bpfx=$(iface_prefix "$_bdev") || return 1
	[ -n "$_bpfx" ] || return 1
	netaddr "$1" "$_bpfx"
}

# Whether this host would accept a datagram sent to that address.
oldstyle_bcast_ok() {  # oldstyle_bcast_ok <client-ip>
	_baddr=$(oldstyle_bcast_addr "$1") || return 1
	ip -4 route show table local 2>/dev/null \
		| grep -qE "^(local|broadcast) $_baddr "
}

directly_reachable() {  # directly_reachable <ip>
	_d=$(route_dev "$1") || return 1
	[ -n "$_d" ] || return 1
	for _i in $SERVER_IF; do
		if [ "$_i" = "$_d" ]; then return 0; fi
	done
	return 1
}

# The first client is "the" client for the probes and the dry run, which only
# ever deal with one at a time.
read -r CLIENT_NAME CLIENT_MAC CLIENT_IP CLIENT_ARCH CLIENT_PAYLOAD <<EOF
$(clients | head -1)
EOF

# Where the extracted .deb trees put their binaries.
findbin() {
	for d in usr/sbin sbin usr/bin bin; do
		[ -x "$PKG/$d/$1" ] && { printf '%s\n' "$PKG/$d/$1"; return 0; }
	done
	return 1
}

is_running() {  # is_running <name>
	pidfile=$RUN/$1.pid
	[ -r "$pidfile" ] || return 1
	pid=$(cat "$pidfile" 2>/dev/null) || return 1
	[ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}
