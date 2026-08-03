#!/bin/sh
# Show what is running, what is listening, and what the Sun-3 will be told.
# "status.sh -f" tails all the logs together, which is what you want on the
# other end of a serial console while the Sun-3 boots.

. "$(dirname -- "$0")/common.sh"

if [ "${1:-}" = "-f" ]; then
	say "tailing all logs (Ctrl-C to stop)"
	exec tail -n 20 -F "$LOG"/rarpd.log "$LOG"/atftpd.log "$LOG"/rpcbind.log \
		"$LOG"/bootparamd.log "$LOG"/unfsd.log 2>/dev/null
fi

printf 'daemon      pid      state\n'
for name in rarpd atftpd rpcbind bootparamd unfsd; do
	if is_running "$name"; then
		printf '%-11s %-8s running\n' "$name" "$(cat "$RUN/$name.pid")"
	else
		printf '%-11s %-8s stopped\n' "$name" '-'
	fi
done

echo
echo 'capabilities:'
for b in rarpd atftpd rpcbind; do
	if [ -x "$SBIN/$b" ]; then
		cap=$(getcap "$SBIN/$b" 2>/dev/null)
		printf '  %-10s %s\n' "$b" "${cap:-NONE -- run root/grant-privileges.sh}"
	fi
done

echo
echo 'listening (want :69 tftp, :111 portmap, :2049 nfs):'
ss -lnup 2>/dev/null | awk 'NR==1 || /:69 |:111 |:2049 /' | sed 's/^/  /'

if is_running rpcbind && [ -x "$SBIN/rpcinfo" ]; then
	echo
	echo 'registered RPC services (want portmapper, bootparam, mountd, nfs):'
	"$SBIN/rpcinfo" -p 127.0.0.1 2>/dev/null | sed 's/^/  /' \
		|| echo '  rpcinfo failed'
fi

echo
echo 'the clients will be told:'
clients | while read -r n m i a; do
	f=$(tftpname "$i" "$a")
	printf '  TFTP file   %-14s %s -> %s\n' "$n" "$f" \
		"$(readlink -f "$TFTPBOOT/$f" 2>/dev/null || echo 'MISSING -- run 03-configure.sh')"
	if directly_reachable "$i"; then
		printf '              %-14s on %s\n' "$n" "$(route_dev "$i")"
	else
		printf '              %-14s NOT reachable on any of "%s": %s\n' \
			"$n" "$SERVER_IF" "$(ip -4 route get "$i" 2>&1 | head -1)"
	fi
done
printf '  RARP        '; sed -n '/^[^#]/p' "$ETC/ethers" 2>/dev/null | sed 's/^/            /;1s/^ *//'
printf '  bootparams  '; sed -n '/^[^#]/p' "$ETC/bootparams" 2>/dev/null | sed 's/^/              /;1s/^ *//'
printf '  export      '; sed -n '/^[^#]/p' "$ETC/exports" 2>/dev/null | sed 's/^/              /;1s/^ *//'

# rpcbind always logs this because /run/rpcbind.sock is root-owned.  It is
# harmless -- rpcbind ignores the failure and libtirpc registers over loopback
# instead -- but it looks alarming in the log, so name it explicitly.
if grep -qs 'cannot bind local' "$LOG/rpcbind.log"; then
	echo
	echo 'note: "rpcbind: cannot bind local: Permission denied" in log/rpcbind.log'
	echo '      is expected when running unprivileged and does not affect booting.'
fi
