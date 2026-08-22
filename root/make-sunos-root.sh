#!/bin/sh
# ===========================================================================
# Unpack a SunOS 4.0.3 root filesystem for a netbooting Sun-2.
#
# Root is needed for exactly one reason: the archive holds 113 device nodes,
# and mknod is privileged.  Unpacked as an ordinary user /dev comes out empty
# and the client cannot open its console.  ../Sun-2_DiskImage/netboot/mkroot
# refuses to run as anyone but root for that reason, and says so.
#
# Nothing here runs as root afterwards.  The NFS server stays an ordinary
# process, which is why this script does a second thing after unpacking:
#
#   mkroot unpacks a tree owned by root, as it is on a real SunOS system.
#   tools/nfs2d.py runs as $OWNER and could then read some of it and write
#   none of it.  So the tree is handed to $OWNER, and nfs2d is told
#   --squash-to-root, which reports every file to the client as owned by
#   root again.  The client sees the ownership SunOS expects; the server can
#   actually do the I/O.
#
#   chown clears setuid and setgid bits, and this tree has 50 of them
#   (/usr/bin/login, /usr/bin/su, ...), so they are recorded first and put
#   back afterwards.
#
# This is destructive: it unpacks over whatever is at the destination.  The
# swap file is left alone if it is already there.
#
# Usage:  sudo root/make-sunos-root.sh [client-name]
#         defaults to the first sunos client in config/sun3boot.conf
# ===========================================================================
set -eu

D=/home/dolbeau2/Sun3_BootDir
OWNER=dolbeau2
MKROOT=$D/../Sun-2_DiskImage/netboot/mkroot

[ "$(id -u)" = 0 ] || { echo "must be run as root" >&2; exit 1; }
[ -x "$MKROOT" ] || { echo "$MKROOT not found" >&2; exit 1; }

# Ask the configuration which client, so the name, address and path cannot
# drift from what the daemons are serving.
NAME=${1:-}
eval "$(sed -n '/^CLIENTS=/,/^.$/p' "$D/config/sun3boot.conf")"
LINE=$(printf '%s\n' "$CLIENTS" | awk -v want="$NAME" \
	'$5 == "sunos" && (want == "" || $1 == want) { print; exit }')
[ -n "$LINE" ] || { echo "no sunos client${NAME:+ called $NAME} in config/sun3boot.conf" >&2; exit 1; }

set -- $LINE
NAME=$1; IP=$3
DEST=$D/nfsroot/$NAME

echo "client $NAME at $IP"
echo "root   $DEST"
echo

"$MKROOT" --hostname "$NAME" --ip "$IP" "$DEST"

echo
echo "handing the tree to $OWNER so an unprivileged NFS server can write it"
SETUID_LIST=$(mktemp)
find "$DEST" -type f -perm /6000 -printf '%m %p\n' >"$SETUID_LIST"
echo "  $(wc -l <"$SETUID_LIST") setuid/setgid files recorded"

chown -R "$OWNER:$OWNER" "$DEST"

while read -r mode path; do
	chmod "$mode" "$path"
done <"$SETUID_LIST"
echo "  modes restored"
rm -f "$SETUID_LIST"

echo
echo "done.  As $OWNER:"
echo "    scripts/03-configure.sh && scripts/stop.sh && scripts/start.sh"
echo "    scripts/selftest.sh"
