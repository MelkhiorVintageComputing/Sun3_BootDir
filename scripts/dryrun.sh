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
# ndbootd finds a first stage per client in ndboot/, under the same name it
# finds the second stage by in tftpboot/ (src/ndbootd-boot1-dir.patch), so
# that is the directory the real daemon is given and the one to rehearse --
# with every sun2, since the point of it is that they differ.
SUN2_CLIENTS=$(clients | while read -r n m i a p; do
	[ "$a" = sun2 ] && printf '%s %s\n' "$m" "$(tftpname "$i" "$a")"
done)
# The NFSv2 server's arguments, and the client it is really for.  Worked out
# by common.sh so that this rehearses exactly what start.sh runs.
NFS2D_ARGS=$(nfs2d_args)
# A netbsd2 client for preference: it is the one with a whole root filesystem
# and a /dev to check, and the checks below are about its export.
NFS2D_CLIENT=$(clients | awk '$5 == "netbsd2" { print $1; exit }')
[ -n "$NFS2D_CLIENT" ] || NFS2D_CLIENT=$(clients | awk '$5 == "sunos" { print $1; exit }')
NFS2D_FILE=$(clients | awk -v n="$NFS2D_CLIENT" '$1 == n { print ($4 == "sun2" ? "vmunix" : "netbsd") }')
NFS2D_SPEC=$(clients | awk '$5 == "netbsd2" { print $1; exit }')
NFS2D_EVERY=$(nfs2d_answers_every_version && echo yes || echo no)

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
UNFSD_PORT='$UNFSD_PORT'
SUN2_CLIENTS='$SUN2_CLIENTS'
NDBOOT='$NDBOOT'
NFSROOT='$NFSROOT'
NFS2D_PORT='$NFS2D_PORT'
NFS2D_TSIZE='$NFS2D_TSIZE'
NFS2D_ARGS='$NFS2D_ARGS'
NFS2D_CLIENT='$NFS2D_CLIENT'
NFS2D_FILE='$NFS2D_FILE'
NFS2D_SPEC='$NFS2D_SPEC'
NFS2D_EVERY='$NFS2D_EVERY'
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
	LAST_PID=$!
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
# nfs2d before unfsd: the portmapper keeps the first registration it is given
# for a program and version, and nfs2d is the one that has to have the low
# MOUNT versions.  Same reason start.sh stands unfsd down entirely when nfs2d
# is answering all of them.
NFS2D_PID=
if [ -n "$NFS2D_CLIENT" ]; then
	# shellcheck disable=SC2086
	start nfs2d python3 "$BOOTDIR/tools/nfs2d.py" \
		--root "$NFSROOT" --port "$NFS2D_PORT" --tsize "$NFS2D_TSIZE" \
		--debug $NFS2D_ARGS || exit 1
	NFS2D_PID=$LAST_PID
fi
if [ "$NFS2D_EVERY" = yes ]; then
	echo '  SKIP  unfsd -- nfs2d answers every MOUNT version, as in start.sh'
else
	start unfsd "$SBIN/unfsd" -d -s -e "$ETC/exports" -n "$UNFSD_PORT" -m "$UNFSD_PORT" || exit 1
fi
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
if [ -z "$SUN2_CLIENTS" ]; then
	echo '  SKIP  no sun2 client configured'
elif [ ! -x "$SBIN/ndbootd" ]; then
	no 'sbin/ndbootd missing -- run scripts/01-build-ndbootd.sh'
else
	# A veth pair is the whole point here: ndbootd needs a real interface
	# to open an AF_PACKET socket on, and inside this namespace we can make
	# one.  This is what actually exercises src/ndbootd-packet.c -- the
	# capability the real daemon needs is not required in here.
	ip link add nd0 type veth peer name nd1 \
		&& ip addr add "$SERVER_IP/24" dev nd0 \
		&& ip link set nd0 up && ip link set nd1 up
	start ndbootd "$SBIN/ndbootd" -d -i nd0 -s "$TFTPBOOT" "$NDBOOT" || exit 1
	# Block 0 is the label; blocks 1-15 are the first stage, and block 16
	# onwards is the second stage ndbootd finds by hex name in tftpboot.
	# Ask as each Sun-2 in turn and check block 1 really is that machine's
	# own first stage: a SunOS one handed NetBSD's bootyy gets nowhere.
	while read -r mac hexname; do
		[ -n "$mac" ] || continue
		if [ ! -e "$NDBOOT/$hexname" ]; then
			no "ndboot/$hexname missing -- run scripts/03-configure.sh"
			continue
		fi
		want=$(od -An -tx1 -N16 "$NDBOOT/$hexname" | tr -d ' \n')
		for blk in 1 16; do
			if out=$(python3 "$BOOTDIR/tools/nd-probe.py" --interface nd1 \
					--client-mac "$mac" --block "$blk" 2>&1); then
				ok "$mac: ND read of block $blk answered"
			else
				no "$mac: ND read of block $blk failed:"
				printf '%s\n' "$out" | sed 's/^/        /'
				sed 's/^/        /' "$LOG/dryrun-ndbootd.log"
				continue
			fi
			[ "$blk" = 1 ] || continue
			got=${out##*first 16: }
			got=${got%%[!0-9a-f]*}
			if [ "$got" = "$want" ]; then
				ok "$mac: block 1 is $(readlink -f "$NDBOOT/$hexname" | sed 's|.*/||')"
			else
				no "$mac: block 1 starts $got, ndboot/$hexname starts $want"
			fi
		done
	done <<CLIENTS
$SUN2_CLIENTS
CLIENTS
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
if [ "$NFS2D_EVERY" = yes ]; then
	echo '  SKIP  unfsd is not running; the NFSv2 check below is what replaces it'
elif out=$(python3 "$BOOTDIR/tools/nfs-probe.py" --server 127.0.0.1 2>&1); then
	ok 'unfsd served the kernel'
	printf '%s\n' "$out" | sed 's/^/        /'
else
	no 'NFS probe failed'
	printf '%s\n' "$out" | sed 's/^/        /'
	sed 's/^/        /' "$LOG/dryrun-unfsd.log"
fi

echo
echo 'NFSv2: what a SunOS or old-NetBSD client gets instead'
if [ -z "$NFS2D_CLIENT" ]; then
	echo '  SKIP  no sunos or netbsd2 client configured'
else
	set -- --server 127.0.0.1 --client "$NFS2D_CLIENT" --file "$NFS2D_FILE" \
		--nfs-version 2
	[ -z "$NFS2D_SPEC" ] || set -- "$@" \
		--check-devices "$NFSROOT/$NFS2D_SPEC/dev/MAKEDEV.spec" \
		--check-setattr --check-rename --check-mount-fallback \
		--check-create-device dev/null
	if out=$(python3 "$BOOTDIR/tools/nfs-probe.py" "$@" 2>&1); then
		ok "nfs2d served $NFS2D_CLIENT over NFSv2"
		printf '%s\n' "$out" | sed 's/^/        /'
	else
		no 'NFSv2 probe failed'
		printf '%s\n' "$out" | sed 's/^/        /'
		sed 's/^/        /' "$LOG/dryrun-nfs2d.log"
	fi
fi

echo
echo 'log rotation: rename the log, SIGHUP, and keep writing'
if [ -z "$NFS2D_PID" ]; then
	echo '  SKIP  nfs2d not running'
else
	# The daemon holds a descriptor, not a name, so a renamed log goes on
	# being written to until it is told to look at the name again.  This is
	# the whole of what logrotate needs, without copytruncate.
	mv "$LOG/dryrun-nfs2d.log" "$LOG/dryrun-nfs2d.log.rotated"
	kill -HUP "$NFS2D_PID"
	sleep 1
	if [ ! -f "$LOG/dryrun-nfs2d.log" ]; then
		no 'SIGHUP did not reopen the log; nothing was created'
	elif grep -q 'on SIGHUP' "$LOG/dryrun-nfs2d.log"; then
		ok 'SIGHUP reopened the log at its own name'
		sed 's/^/        /' "$LOG/dryrun-nfs2d.log"
	else
		no 'a new log appeared but nfs2d did not say it reopened one:'
		sed 's/^/        /' "$LOG/dryrun-nfs2d.log"
	fi
	# Put the whole trace back where a reader would look for it.
	cat "$LOG/dryrun-nfs2d.log" >>"$LOG/dryrun-nfs2d.log.rotated"
	mv "$LOG/dryrun-nfs2d.log.rotated" "$LOG/dryrun-nfs2d.log"
	kill -HUP "$NFS2D_PID"
fi

echo
if [ "$fail" -eq 0 ]; then
	echo 'Dry run passed: TFTP, ND, portmap+bootparams and both NFS versions
all behave.'
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
