#!/bin/sh
# Start the netboot daemons.  Nothing here runs as root.
#
#   start.sh          everything (milestone M3: the Sun-3 boots a kernel)
#   start.sh m1       rarpd + atftpd only -- the PROM downloads netboot
#   start.sh m2       + rpcbind + bootparamd -- netboot finds its root
#   start.sh <name>   just one of: rarpd ndbootd atftpd rpcbind bootparamd
#                     unfsd nfs2d
#
# ndbootd is started only when the table has a sun2 in it; nothing else needs
# it, and it will not run without a first-stage boot program to serve.
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

start_ndbootd() {
	# Only a Sun-2 speaks ND, so there is nothing to run without one.
	if [ -z "$(clients | awk '$4 == "sun2"')" ]; then
		say "no sun2 client configured; not starting ndbootd"
		return 0
	fi
	sun2_ifs=$(clients | awk '$4 == "sun2" { print $3 }' | while read -r ip; do
		route_dev "$ip" || true
		echo
	done | sed '/^$/d' | sort -u)
	# Everything below is a warn-and-skip rather than a die: ndbootd serves
	# only the Sun-2, and a Sun-3 stack should still come up without it.
	if [ ! -x "$SBIN/ndbootd" ]; then
		warn "sbin/ndbootd missing -- run scripts/01-build-ndbootd.sh; no sun2 can boot"
		return 0
	fi
	case $(getcap "$SBIN/ndbootd" 2>/dev/null) in
	*cap_net_raw*) ;;
	*)
		warn "sbin/ndbootd lacks cap_net_raw, so no sun2 can get an address."
		warn "  An administrator must run root/grant-privileges.sh again"
		warn "  (ndbootd is new, and capabilities do not survive a rebuild)."
		return 0 ;;
	esac
	# The first stage differs entirely between the two payloads, and ndbootd
	# serves exactly one.  NetBSD's bootyy speaks ND onwards; SunOS's sun2.bb
	# stops using ND immediately and goes off to RARP and TFTP instead.
	sun2_payloads=$(clients | awk '$4 == "sun2" { print $5 }' | sort -u)
	set -- $sun2_payloads
	if [ $# -gt 1 ]; then
		warn "sun2 clients ask for different payloads ($*) -- ndbootd serves one"
		warn "  first stage, so using $1; give the others their own ndbootd"
	fi
	case $1 in
	sunos) boot1=$BOOTDIR/payload/sunos-sun2.bb ;;
	*)     boot1=$BOOTDIR/payload/sun2-bootyy ;;
	esac
	if [ ! -f "$boot1" ]; then
		warn "$boot1 missing -- run scripts/02-fetch-payload.sh; no sun2 can boot"
		return 0
	fi
	say "ndbootd first stage: ${boot1#"$BOOTDIR"/} ($1)"

	# ndbootd binds one interface per instance and has no -a.  With every
	# sun2 on one segment that is fine; more than one would need more than
	# one instance, which nothing here manages yet.
	set -- $sun2_ifs
	if [ $# -eq 0 ]; then
		warn "no sun2 client is reachable on any of \"$SERVER_IF\"; not starting ndbootd"
		return 0
	fi
	if [ $# -gt 1 ]; then
		warn "sun2 clients span $* -- ndbootd serves one interface, using $1"
	fi
	# -s tftpboot: find each client's second stage by its hex name there,
	#    exactly as atftpd does for a Sun-3.
	# The trailing argument is the first stage, ND blocks 1-15.
	spawn ndbootd "$SBIN/ndbootd" -d -i "$1" -s "$TFTPBOOT" "$boot1"
}

start_nfs2d() {
	# SunOS 4.0.3 is from 1989 and speaks NFS version 2; unfs3 serves version
	# 3 and nothing else.  They register under different version numbers, so
	# both run at once and each client uses the one it can talk to.
	if [ -z "$(clients | awk '$5 == "sunos"')" ]; then
		say "no sunos client configured; not starting nfs2d"
		return 0
	fi
	is_running rpcbind || warn "rpcbind is not running; nfs2d will fail to register"
	# Each sunos client swaps over NFS, so its swap file -- and nothing else
	# in the export -- has to be writable.
	set --
	squash=
	for n in $(clients | awk '$5 == "sunos" { print $1 }'); do
		if [ -f "$NFSROOT/$n/etc/rc.boot" ]; then
			# A real root filesystem: the whole tree is writable, and
			# it is owned by us rather than by root, so tell nfs2d to
			# report root ownership -- which is what SunOS expects.
			set -- "$@" --writable-tree "$NFSROOT/$n"
			squash=--squash-to-root
			say "$n has a SunOS root filesystem: serving it read-write"
		else
			set -- "$@" --writable "$NFSROOT/$n/swap"
		fi
	done
	[ -z "$squash" ] || set -- "$@" "$squash"
	# SunOS sends NFS to 2049 without asking the portmapper, so this has to
	# be the server sitting there.
	spawn nfs2d python3 "$BOOTDIR/tools/nfs2d.py" \
		--root "$NFSROOT" --port "$NFS2D_PORT" --tsize "$NFS2D_TSIZE" \
		--debug "$@"
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
	# -r is the default router reported in the WHOAMI reply.  Left to itself
	# bootparamd answers with whatever the server's own name resolves to,
	# which is 127.0.0.1 here -- telling the client to route via its own
	# loopback.  Name the address the client actually reaches us on.
	spawn bootparamd unshare --user --map-root-user --mount -- /bin/sh -c \
		"mount --bind '$ETC/hosts' /etc/hosts && exec '$SBIN/rpc.bootparamd' -d -r '$SERVER_IP' -f '$ETC/bootparams'"
}

start_unfsd() {
	[ -x "$SBIN/unfsd" ] || die "sbin/unfsd missing -- run scripts/01-build-unfs3.sh"
	[ -r "$ETC/exports" ] || die "etc/exports missing -- run scripts/03-configure.sh"
	is_running rpcbind || warn "rpcbind is not running; unfsd will fail to register"
	# -s: serve everything as the invoking user.  We are not root, so no uid
	#     switching can happen anyway; this just makes the reported ownership
	#     consistent.  Read-only is all the bootloader needs.
	# Not 2049: that is where a client which does not consult the portmapper
	# expects to find NFS, and SunOS is such a client.  NetBSD asks for both
	# mountd and nfs by program number, so it follows us anywhere.
	spawn unfsd "$SBIN/unfsd" -d -s -e "$ETC/exports" \
		-n "$UNFSD_PORT" -m "$UNFSD_PORT"
}

case "${1:-all}" in
m1)         start_rarpd; start_ndbootd; start_atftpd ;;
m2)         start_rarpd; start_ndbootd; start_atftpd; start_rpcbind; start_bootparamd ;;
m3|all)     start_rarpd; start_ndbootd; start_atftpd; start_rpcbind; start_bootparamd
            start_unfsd; start_nfs2d ;;
rarpd)      start_rarpd ;;
ndbootd)    start_ndbootd ;;
atftpd)     start_atftpd ;;
rpcbind)    start_rpcbind ;;
bootparamd) start_bootparamd ;;
unfsd)      start_unfsd ;;
nfs2d)      start_nfs2d ;;
*) die "usage: $0 [m1|m2|m3|all|rarpd|ndbootd|atftpd|rpcbind|bootparamd|unfsd|nfs2d]" ;;
esac

echo
"$BOOTDIR/scripts/status.sh" || true
