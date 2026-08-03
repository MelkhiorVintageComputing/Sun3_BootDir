#!/bin/sh
# Start the netboot daemons.  Nothing here runs as root.
#
#   start.sh          everything (milestone M3: the Sun-3 boots a kernel)
#   start.sh m1       rarpd + atftpd only -- the PROM downloads netboot
#   start.sh m2       + rpcbind + bootparamd -- netboot finds its root
#   start.sh <name>   just one of: rarpd atftpd rpcbind bootparamd unfsd
#
# Bringing the stack up in stages makes a failure point at one protocol
# instead of five.

. "$(dirname -- "$0")/common.sh"

mkdir -p "$RUN" "$LOG"

spawn() {  # spawn <name> <command...>
	name=$1; shift
	if is_running "$name"; then
		say "$name already running (pid $(cat "$RUN/$name.pid"))"
		return 0
	fi
	rm -f "$RUN/$name.pid"
	nohup "$@" >>"$LOG/$name.log" 2>&1 &
	echo $! >"$RUN/$name.pid"
	sleep 1
	if is_running "$name"; then
		say "$name started (pid $(cat "$RUN/$name.pid")), logging to log/$name.log"
	else
		rm -f "$RUN/$name.pid"
		warn "$name died immediately; last lines of log/$name.log:"
		tail -15 "$LOG/$name.log" >&2
		return 1
	fi
}

need_cap() {  # need_cap <binary> <capability>
	[ -x "$SBIN/$1" ] || die "sbin/$1 missing -- run scripts/00-fetch-packages.sh"
	case $(getcap "$SBIN/$1" 2>/dev/null) in
	*"$2"*) return 0 ;;
	esac
	die "sbin/$1 lacks $2 -- an administrator must run root/grant-privileges.sh once"
}

start_rarpd() {
	need_cap rarpd cap_net_raw
	if [ "$(readlink -f /etc/ethers 2>/dev/null)" != "$ETC/ethers" ]; then
		warn "/etc/ethers is not linked to etc/ethers; rarpd will not know its clients"
	fi
	# rarpd enumerates interfaces once, at startup, and skips any that are not
	# IFF_UP.  Carrier is a different matter: a bridge over an emulator's tap
	# has none until the emulator runs, and rarpd is quite happy with that.
	for i in $SERVER_IF; do
		if [ ! -e "/sys/class/net/$i" ]; then
			warn "$i does not exist; rarpd will not serve it"
		elif ! iface_up "$i"; then
			warn "$i is administratively down, so rarpd will not enumerate it."
			warn "  Bring it up, then: scripts/stop.sh rarpd && scripts/start.sh rarpd"
		fi
	done
	# rarpd binds a single interface per instance, so a table spread over more
	# than one needs -a.  It then answers on whichever interface the request
	# arrived on, which is exactly what we want.
	# -b makes rarpd answer only when the matching boot file exists, which
	# turns a missing tftpboot entry into silence rather than a half-boot.
	set -- $SERVER_IF
	if [ $# -eq 1 ]; then
		spawn rarpd "$SBIN/rarpd" -d -v -b "$TFTPBOOT" "$1"
	else
		spawn rarpd "$SBIN/rarpd" -d -v -a -b "$TFTPBOOT"
	fi
}

start_atftpd() {
	need_cap atftpd cap_net_bind_service
	# The 1986 PROM does not do RFC 2347 option negotiation; refuse to
	# acknowledge options so nothing unexpected ends up in the reply.
	spawn atftpd "$SBIN/atftpd" --daemon --no-fork --port 69 --verbose=7 --listen-local \
		--no-blksize --no-tsize --no-timeout --no-multicast \
		--no-windowsize \
		--logfile "$LOG/atftpd.log" "$TFTPBOOT"
}

start_rpcbind() {
	need_cap rpcbind cap_net_bind_service
	# -i  our unprivileged daemons register from ephemeral source ports,
	#     which rpcbind's loopback check would otherwise reject.
	# No -h: it must stay on 0.0.0.0 or it never sees the 255.255.255.255
	#     broadcast netboot uses to find bootparamd.
	# Indirect-call support comes from the --enable-rmtcalls build, see
	# scripts/01-build-rpcbind.sh.
	spawn rpcbind "$SBIN/rpcbind" -f -d -i
}

start_bootparamd() {
	[ -x "$SBIN/rpc.bootparamd" ] || die "sbin/rpc.bootparamd missing -- run scripts/00-fetch-packages.sh"
	[ -r "$ETC/bootparams" ] || die "etc/bootparams missing -- run scripts/03-configure.sh"
	is_running rpcbind || warn "rpcbind is not running; bootparamd will fail to register"
	# bootparamd identifies the client with gethostbyaddr(), so it needs an
	# /etc/hosts entry.  Rather than edit the real one (root), give this one
	# daemon a private mount namespace with ours bind-mounted over it.  The
	# network namespace is untouched, so it still registers and serves
	# normally.
	spawn bootparamd unshare --user --map-root-user --mount -- /bin/sh -c \
		"mount --bind '$ETC/hosts' /etc/hosts && exec '$SBIN/rpc.bootparamd' -d -f '$ETC/bootparams'"
}

start_unfsd() {
	[ -x "$SBIN/unfsd" ] || die "sbin/unfsd missing -- run scripts/01-build-unfs3.sh"
	[ -r "$ETC/exports" ] || die "etc/exports missing -- run scripts/03-configure.sh"
	is_running rpcbind || warn "rpcbind is not running; unfsd will fail to register"
	# -s: serve everything as the invoking user.  We are not root, so no uid
	#     switching can happen anyway; this just makes the reported ownership
	#     consistent.  Read-only is all the bootloader needs.
	spawn unfsd "$SBIN/unfsd" -d -s -e "$ETC/exports" -n 2049 -m 2049
}

case "${1:-all}" in
m1)         start_rarpd; start_atftpd ;;
m2)         start_rarpd; start_atftpd; start_rpcbind; start_bootparamd ;;
m3|all)     start_rarpd; start_atftpd; start_rpcbind; start_bootparamd; start_unfsd ;;
rarpd)      start_rarpd ;;
atftpd)     start_atftpd ;;
rpcbind)    start_rpcbind ;;
bootparamd) start_bootparamd ;;
unfsd)      start_unfsd ;;
*) die "usage: $0 [m1|m2|m3|all|rarpd|atftpd|rpcbind|bootparamd|unfsd]" ;;
esac

echo
"$BOOTDIR/scripts/status.sh" || true
