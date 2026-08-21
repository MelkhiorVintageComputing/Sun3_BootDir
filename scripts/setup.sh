#!/bin/sh
# Build the whole environment from an empty directory.  Needs no privilege.
#
# Afterwards, an administrator runs root/grant-privileges.sh once, and then
# scripts/start.sh brings the stack up.

. "$(dirname -- "$0")/common.sh"

for s in 00-fetch-packages 01-build-rpcbind 01-build-atftpd 01-build-unfs3 \
         01-build-ndbootd 02-fetch-payload 03-configure; do
	echo
	echo "########## $s"
	"$BOOTDIR/scripts/$s.sh"
done

cat <<EOF

##########

Unprivileged setup is complete.  Two steps left:

  1. Have an administrator review and run, once:
         sudo $BOOTDIR/root/grant-privileges.sh

  2. Then:
         $BOOTDIR/scripts/start.sh
         $BOOTDIR/scripts/selftest.sh
EOF
