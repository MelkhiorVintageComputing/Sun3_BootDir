#!/bin/sh
# Rehearse the RPC half of the boot chain without any privilege at all.
#
# Inside an unprivileged user + network namespace we are root over a private
# loopback, so ports 69 and 111 are ours for free.  That lets us prove the
# parts that are easy to get wrong -- rpcbind's indirect-call forwarding,
# bootparamd's hostname lookup, unfs3 answering MOUNT v3 -- *before* asking an
# administrator to grant any capabilities.
#
# What this cannot cover: RARP (needs a real Ethernet interface) and anything
# involving the real LAN.  Those are what scripts/selftest.sh is for once
# root/grant-privileges.sh has been run.

. "$(dirname -- "$0")/common.sh"

for b in rpcbind rpc.bootparamd unfsd atftpd tftp; do
	[ -e "$SBIN/$b" ] || die "sbin/$b missing -- run scripts/setup.sh first"
done
[ -r "$ETC/bootparams" ] || die "etc/bootparams missing -- run scripts/03-configure.sh"
# The namespace has its own network but shares run/, and rpcbind refuses to
# start when it finds another instance's lock file there.
if is_running rpcbind; then
	die "the live stack is running -- run scripts/stop.sh first"
fi

# Worked out here rather than inside the heredoc, where the quoting needed to
# get an awk program through unscathed is not worth it.
SUN2_MAC=$(clients | awk '$4 == "sun2" { print $2; exit }')

say "entering a private user+network namespace"
# --pid --fork --kill-child means every daemon started inside dies with this
# script, even if it is interrupted.  Without it a leftover rpcbind keeps the
# lock file and every later run fails with "another rpcbind is already running".
exec unshare --user --map-root-user --mount --net --pid --fork --kill-child \
	-- /bin/sh -s <<INNER
set -eu
BOOTDIR='$BOOTDIR'
SBIN='$SBIN'
ETC='$ETC'
LOG='$LOG'
TFTPBOOT='$TFTPBOOT'
NAME='$(tftpname "$CLIENT_IP" "$CLIENT_ARCH")'
CLIENT_IP='$CLIENT_IP'
SERVER_IP='$SERVER_IP'
SUN2_MAC='$SUN2_MAC'
INNER_SCRIPT=1
$(cat <<'SCRIPT'

fail=0
ok() { printf '  PASS  %s\n' "$*"; }
no() { printf '  FAIL  %s\n' "$*"; fail=1; }

cleanup() { kill $PIDS 2>/dev/null || true; }
PIDS=
trap cleanup EXIT

ip link set lo up
mount --bind "$ETC/hosts" /etc/hosts

start() {  # start <name> <command...>
	name=$1; shift
	"$@" >"$LOG/dryrun-$name.log" 2>&1 &
	PIDS="$PIDS $!"
	sleep 1
	kill -0 $! 2>/dev/null || {
		no "$name failed to start:"
		sed 's/^/        /' "$LOG/dryrun-$name.log"
		return 1
	}
	ok "$name started"
}

echo
echo 'starting daemons on a private loopback'
start rpcbind    "$SBIN/rpcbind" -f -d -i || exit 1
start bootparamd "$SBIN/rpc.bootparamd" -d -r "$SERVER_IP" -f "$ETC/bootparams" || exit 1
start unfsd      "$SBIN/unfsd" -d -s -e "$ETC/exports" -n 2049 -m 2049 || exit 1
# --user/--group are only needed here: inside the namespace our uid maps to 0,
# so atftpd thinks it is root and insists on dropping privileges to a user that
# does not exist in the map.  Outside, it sees a plain uid and skips all of
# that, which is why start.sh passes neither.
start atftpd     "$SBIN/atftpd" --daemon --no-fork --port 69 --verbose=7 --listen-local \
	--user root --group root \
	--no-blksize --no-tsize --no-timeout --no-multicast --no-windowsize \
	--logfile "$LOG/dryrun-atftpd.log" "$TFTPBOOT" || exit 1

echo
echo 'RPC registrations'
reg=$("$SBIN/rpcinfo" -p 127.0.0.1 2>&1 || true)
for svc in 'portmapper 100000' 'bootparam 100026' 'mountd 100005' 'nfs 100003'; do
	n=${svc% *}; p=${svc#* }
	case $reg in
	*"  $p  "*) ok "$n ($p) registered" ;;
	*)          no "$n ($p) missing"; printf '%s\n' "$reg" | sed 's/^/        /' ;;
	esac
done

echo
echo "TFTP GET $NAME (a bare relative name, exactly what the PROM sends)"
tmp=$(mktemp -d)
( cd "$tmp" && "$SBIN/tftp" 127.0.0.1 -c get "$NAME" >/dev/null 2>&1 ) || true
if cmp -s "$tmp/$NAME" "$(readlink -f "$TFTPBOOT/$NAME")"; then
	ok "$NAME served, $(wc -c <"$tmp/$NAME") bytes, matches the netboot image"
else
	no "TFTP GET $NAME did not return the netboot image"
	sed 's/^/        /' "$LOG/dryrun-atftpd.log"
fi
rm -rf "$tmp"

echo
echo "TFTP GET $NAME by broadcast (what the PROM actually does)"
if out=$(python3 "$BOOTDIR/tools/tftp-bcast-probe.py" --dest 127.255.255.255 --blocks 4 2>&1); then
	ok 'broadcast read request answered from a usable source address'
else
	no 'broadcast read request failed:'
	printf '%s\n' "$out" | sed 's/^/        /'
	echo '        atftpd must be the patched build and started with --listen-local'
fi

echo
echo 'ND (Sun-2) over a veth pair'
if [ -z "$SUN2_MAC" ]; then
	echo '  SKIP  no sun2 client configured'
elif [ ! -x "$SBIN/ndbootd" ]; then
	no 'sbin/ndbootd missing -- run scripts/01-build-ndbootd.sh'
elif [ ! -f "$BOOTDIR/payload/sun2-bootyy" ]; then
	no 'payload/sun2-bootyy missing -- run scripts/02-fetch-payload.sh'
else
	# A veth pair is the whole point here: ndbootd needs a real interface
	# to open an AF_PACKET socket on, and inside this namespace we can make
	# one.  This is what actually exercises src/ndbootd-packet.c -- the
	# capability the real daemon needs is not required in here.
	ip link add nd0 type veth peer name nd1 \
		&& ip addr add "$SERVER_IP/24" dev nd0 \
		&& ip link set nd0 up && ip link set nd1 up
	start ndbootd "$SBIN/ndbootd" -d -i nd0 -s "$TFTPBOOT" \
		"$BOOTDIR/payload/sun2-bootyy" || exit 1
	# Block 0 is the label; blocks 1-15 are the first stage, and block 16
	# onwards is the second stage ndbootd finds by hex name in tftpboot.
	for blk in 1 16; do
		if out=$(python3 "$BOOTDIR/tools/nd-probe.py" --interface nd1 \
				--client-mac "$SUN2_MAC" --block "$blk" 2>&1); then
			ok "ND read of block $blk answered"
			printf '%s\n' "$out" | sed 's/^/        /'
		else
			no "ND read of block $blk failed:"
			printf '%s\n' "$out" | sed 's/^/        /'
			sed 's/^/        /' "$LOG/dryrun-ndbootd.log"
		fi
	done
fi

echo
echo 'bootparams WHOAMI/GETFILE over PMAPPROC_CALLIT'
if out=$(python3 "$BOOTDIR/tools/bp-probe.py" --server 127.0.0.1 "$CLIENT_IP" 2>&1); then
	ok 'bootparamd answered'
	printf '%s\n' "$out" | sed 's/^/        /'
else
	no 'bootparams probe failed'
	printf '%s\n' "$out" | sed 's/^/        /'
	sed 's/^/        /' "$LOG/dryrun-bootparamd.log"
fi

echo
echo 'NFSv3 mount, lookup and read of the kernel'
if out=$(python3 "$BOOTDIR/tools/nfs-probe.py" --server 127.0.0.1 2>&1); then
	ok 'unfsd served the kernel'
	printf '%s\n' "$out" | sed 's/^/        /'
else
	no 'NFS probe failed'
	printf '%s\n' "$out" | sed 's/^/        /'
	sed 's/^/        /' "$LOG/dryrun-unfsd.log"
fi

echo
if [ "$fail" -eq 0 ]; then
	echo 'Dry run passed: TFTP, ND, portmap+bootparams and NFSv3 all behave.'
	echo 'What is left to prove on the real network is RARP, and that these'
	echo 'same daemons can bind ports 69 and 111 outside the namespace --'
	echo 'i.e. root/grant-privileges.sh.'
else
	echo 'Dry run FAILED; see log/dryrun-*.log.'
fi
exit "$fail"
SCRIPT
)
INNER
