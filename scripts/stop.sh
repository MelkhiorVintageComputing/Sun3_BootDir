#!/bin/sh
# Stop the netboot daemons.  stop.sh [name...]; with no arguments, all of them.

. "$(dirname -- "$0")/common.sh"

DAEMONS=${*:-unfsd bootparamd rpcbind atftpd rarpd}

for name in $DAEMONS; do
	if ! is_running "$name"; then
		rm -f "$RUN/$name.pid"
		say "$name not running"
		continue
	fi
	pid=$(cat "$RUN/$name.pid")
	kill "$pid" 2>/dev/null || true
	n=0
	while is_running "$name" && [ "$n" -lt 20 ]; do
		sleep 0.25
		n=$((n + 1))
	done
	if is_running "$name"; then
		warn "$name (pid $pid) ignored SIGTERM; sending SIGKILL"
		kill -9 "$pid" 2>/dev/null || true
		sleep 0.5
	fi
	rm -f "$RUN/$name.pid"
	say "$name stopped"
done
