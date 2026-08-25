#!/bin/sh
# Build the whole environment from an empty directory.  Needs no privilege.
#
# Afterwards, an administrator runs root/grant-privileges.sh once, and then
# scripts/start.sh brings the stack up.

. "$(dirname -- "$0")/common.sh"

run() {
	_s=$1; shift
	echo
	echo "########## $_s${1:+ $1}"
	"$BOOTDIR/scripts/$_s.sh" "$@"
}

for s in 00-fetch-packages 01-build-rpcbind 01-build-atftpd 01-build-unfs3 \
         01-build-ndbootd 02-fetch-payload; do
	run "$s"
done

# A netbsd2 client needs a root filesystem unpacked, which needs no privilege
# and so belongs here -- but only when there is not one already.  The script
# unpacks over whatever it finds, and the machine may be running out of it.
for n in $(clients | awk '$5 == "netbsd2" { print $1 }'); do
	if [ -f "$NFSROOT/$n/etc/rc.conf" ]; then
		say "$n already has a NetBSD root filesystem; leaving it alone"
	else
		run 04-make-netbsd2-root "$n"
	fi
done

run 03-configure

cat <<EOF

##########

Unprivileged setup is complete.  Two steps left:

  1. Have an administrator review and run, once:
         sudo $BOOTDIR/root/grant-privileges.sh

  2. Then:
         $BOOTDIR/scripts/start.sh
         $BOOTDIR/scripts/selftest.sh
EOF
