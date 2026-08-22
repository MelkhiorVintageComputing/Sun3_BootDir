#!/bin/sh
# Exercise the whole boot chain from this host, with no Sun-3 involved.
#
# Everything the Sun-3 does can be checked locally except RARP, which needs a
# raw socket to generate.  That one is verified by watching log/rarpd.log while
# the machine actually boots (scripts/status.sh -f).

. "$(dirname -- "$0")/common.sh"

pass=0
fail=0
ok()   { printf '  PASS  %s\n' "$*"; pass=$((pass + 1)); }
no()   { printf '  FAIL  %s\n' "$*"; fail=$((fail + 1)); }
skip() { printf '  SKIP  %s\n' "$*"; }

NAME=$(tftpname "$CLIENT_IP" "$CLIENT_ARCH")

echo '1. capabilities'
caps='rarpd cap_net_raw:atftpd cap_net_bind_service:rpcbind cap_net_bind_service'
# ndbootd only exists when there is a Sun-2 to serve.
if [ -n "$(clients | awk '$4 == "sun2"')" ]; then
	caps="$caps:ndbootd cap_net_raw"
fi
old_ifs=$IFS; IFS=:
for pair in $caps; do
	IFS=$old_ifs
	b=${pair% *}; c=${pair#* }
	if [ ! -x "$SBIN/$b" ]; then
		no "sbin/$b is missing (run scripts/00-fetch-packages.sh)"
	elif getcap "$SBIN/$b" 2>/dev/null | grep -q "$c"; then
		ok "sbin/$b has $c"
	else
		no "sbin/$b lacks $c (run root/grant-privileges.sh as root)"
	fi
	IFS=:
done
IFS=$old_ifs
if [ "$(readlink -f /etc/ethers 2>/dev/null)" = "$ETC/ethers" ]; then
	ok "/etc/ethers -> etc/ethers"
else
	no "/etc/ethers is not linked to etc/ethers (run root/grant-privileges.sh)"
fi

# Everything after RARP is routed IP.  A client the host would answer through a
# gateway RARPs successfully and then stalls, which reads as a TFTP fault.
# Reading from a heredoc, not a pipe: a pipe would run the loop in a subshell
# and the pass/fail counters would not survive it.
while read -r n m i a p; do
	if directly_reachable "$i"; then
		dev=$(route_dev "$i")
		if ! iface_up "$dev"; then
			no "$n ($i) is on $dev, which is administratively down"
		elif iface_carrier "$dev"; then
			ok "$n ($i) is directly reachable on $dev"
		else
			# A bridge with nothing attached yet.  rarpd has already
			# enumerated it, so this resolves itself when the client
			# (or the emulator behind it) appears.
			ok "$n ($i) is directly reachable on $dev (no carrier yet)"
		fi
	else
		no "$n ($i) is not on any of \"$SERVER_IF\": $(ip -4 route get "$i" 2>&1 | head -1)"
	fi
	# SunOS broadcasts its bootparams request to the network address, which
	# Linux drops unless that address is configured.  Nothing else notices:
	# the request never reaches a daemon, so no log anywhere shows it.
	if [ "$p" = sunos ]; then
		b=$(oldstyle_bcast_addr "$i" 2>/dev/null || echo '?')
		if oldstyle_bcast_ok "$i"; then
			ok "$n: this host accepts the old-style broadcast $b"
		else
			no "$n: $b is not accepted here, so its bootparams request will be dropped"
			echo "        run root/allow-oldstyle-broadcast.sh as root (it does not survive a reboot)"
		fi
	fi
done <<EOF
$(clients)
EOF

echo
echo '2. daemons'
for d in rarpd atftpd rpcbind bootparamd unfsd; do
	if is_running "$d"; then ok "$d running"; else no "$d not running"; fi
done
if [ -n "$(clients | awk '$4 == "sun2"')" ]; then
	if is_running ndbootd; then
		ok 'ndbootd running (a sun2 is configured)'
	else
		no 'ndbootd not running, so no sun2 can get an address'
	fi
fi
if [ -n "$(clients | awk '$5 == "sunos"')" ]; then
	if is_running nfs2d; then
		ok 'nfs2d running (a sunos client needs NFSv2)'
	else
		no 'nfs2d not running, so SunOS cannot read its kernel'
	fi
fi

echo
echo '3. listening sockets'
listening=$(ss -lnu 2>/dev/null)
for port in 69 111 2049; do
	if printf '%s\n' "$listening" | grep -q ":$port "; then
		ok "udp/$port bound"
	else
		no "udp/$port not bound"
	fi
done

echo
echo '4. TFTP: fetch every client'"'"'s file the way the PROM does'
if [ ! -x "$SBIN/tftp" ]; then
	skip 'no tftp client (run scripts/00-fetch-packages.sh)'
elif ! is_running atftpd; then
	skip 'atftpd not running'
else
	tmp=$(mktemp -d)
	while read -r n m i a p; do
		f=$(tftpname "$i" "$a")
		# A client with no boot program has a placeholder here instead of
		# a symlink.  Nothing fetches it, so serving it would prove
		# nothing; just note that the name is reserved and move on.
		if [ ! -L "$TFTPBOOT/$f" ]; then
			if [ -f "$TFTPBOOT/$f" ]; then
				ok "$f ($n, $a) is a placeholder -- no boot program for this arch yet"
			else
				no "$f ($n) is missing"
			fi
			continue
		fi
		# Deliberately a bare relative name with no path: that is all the
		# PROM sends, and it is the thing that breaks under a TFTP server
		# expecting a chroot.
		rm -f "$tmp/$f"
		( cd "$tmp" && "$SBIN/tftp" 127.0.0.1 -c get "$f" >/dev/null 2>&1 ) || true
		if [ ! -s "$tmp/$f" ]; then
			no "TFTP GET $f ($n) returned nothing"
		elif cmp -s "$tmp/$f" "$(readlink -f "$TFTPBOOT/$f")"; then
			ok "TFTP GET $f ($n) matches $(basename "$(readlink -f "$TFTPBOOT/$f")") ($(wc -c <"$tmp/$f") bytes)"
		else
			no "TFTP GET $f ($n) returned different content than tftpboot/$f"
		fi
	done <<EOF
$(clients)
EOF
	rm -rf "$tmp"
fi

echo
echo "5. TFTP by broadcast: the PROM does not know the server's address"
if ! is_running atftpd; then
	skip 'atftpd not running'
elif out=$(python3 "$BOOTDIR/tools/tftp-bcast-probe.py" --blocks 4 2>&1); then
	ok 'broadcast read request answered from a usable source address'
else
	no 'broadcast read request failed:'
	printf '%s\n' "$out" | sed 's/^/        /'
	echo '        atftpd must be the patched build (scripts/01-build-atftpd.sh)'
	echo '        and started with --listen-local'
fi

echo
echo '6. RPC registrations'
if [ ! -x "$SBIN/rpcinfo" ] || ! is_running rpcbind; then
	skip 'rpcbind not running'
else
	reg=$("$SBIN/rpcinfo" -p 127.0.0.1 2>/dev/null || true)
	for svc in 'bootparam 100026' 'mountd 100005' 'nfs 100003'; do
		n=${svc% *}; p=${svc#* }
		if printf '%s\n' "$reg" | grep -q "^ *$p "; then
			ok "$n ($p) registered"
		else
			no "$n ($p) not registered with the portmapper"
		fi
	done
fi

echo
echo '7. bootparams over PMAPPROC_CALLIT (what netboot really does)'
if ! is_running bootparamd || ! is_running rpcbind; then
	skip 'rpcbind or bootparamd not running'
elif python3 "$BOOTDIR/tools/bp-probe.py" >"$LOG/bp-probe.out" 2>&1; then
	ok 'WHOAMI and GETFILE root both answered'
	sed 's/^/        /' "$LOG/bp-probe.out"
else
	no 'bootparams probe failed:'
	sed 's/^/        /' "$LOG/bp-probe.out"
fi

echo
echo "8. NFSv3: every client's root, and the kernel it will ask for"
if ! is_running unfsd; then
	skip 'unfsd not running'
else
	while read -r n m i a p; do
		# A Sun-2 PROM passes "vmunix" to netboot; a Sun-3 asks for
		# "netbsd".  Check the name that machine will actually use, over
		# the NFS version it can actually speak -- SunOS 4.0.3 is six
		# years older than NFSv3.
		case $a in
		sun2) want=vmunix ;;
		*)    want=netbsd ;;
		esac
		case $p in
		sunos) vers=2 ;;
		*)     vers=3 ;;
		esac
		if python3 "$BOOTDIR/tools/nfs-probe.py" --client "$n" --file "$want" \
				--nfs-version "$vers" >"$LOG/nfs-probe-$n.out" 2>&1; then
			ok "$n: mounted its root and read $want over NFSv$vers"
		else
			no "$n: could not read $want from its root over NFSv$vers:"
			sed 's/^/        /' "$LOG/nfs-probe-$n.out"
		fi
	done <<EOF
$(clients)
EOF
fi

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
if [ "$fail" -gt 0 ] && ! getcap "$SBIN/rpcbind" 2>/dev/null | grep -q cap_net; then
	cat <<-EOF

	The capabilities have not been granted yet, so nothing can bind ports 69
	or 111 and most of this could not run.  Until an administrator gets to
	root/grant-privileges.sh, scripts/dryrun.sh exercises the same stack in a
	private namespace and needs no privilege at all.
	EOF
fi
if [ "$fail" -eq 0 ]; then
	cat <<-EOF

	Everything testable from this host works.  RARP is the one step that
	cannot be generated without a second capability, so power on the Sun-3,
	type

	    >b le()

	at the PROM monitor and watch scripts/status.sh -f.
	EOF
fi
[ "$fail" -eq 0 ]
