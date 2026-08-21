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

DIST=$BOOTDIR/dist
PKG=$BOOTDIR/pkg
SBIN=$BOOTDIR/sbin
ETC=$BOOTDIR/etc
TFTPBOOT=$BOOTDIR/tftpboot
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
		*) die "client $n: payload must be netbsd or sunos, not '$p'" ;;
		esac
	done || exit 1
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
