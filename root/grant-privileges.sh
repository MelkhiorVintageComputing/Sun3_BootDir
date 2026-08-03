#!/bin/sh
# ===========================================================================
# The ONLY part of the Sun-3 netboot environment that needs root.
# Review it, run it once as root, and you are done.  Everything else in
# /home/dolbeau2/Sun3_BootDir runs as an ordinary user.
#
# Three daemons need one capability each; nothing runs as root afterwards:
#   rarpd    RARP replies come from an AF_PACKET socket        -> CAP_NET_RAW
#   atftpd   TFTP listens on udp/69                            -> CAP_NET_BIND_SERVICE
#   rpcbind  the portmapper listens on udp+tcp/111             -> CAP_NET_BIND_SERVICE
#
# The symlink exists because rarpd has no option to relocate its MAC->IP
# table; pointing /etc/ethers into the boot directory keeps that table
# editable without root, so this script never has to be run again when the
# Sun-3's address changes.
#
# To undo everything:  setcap -r $D/sbin/{rarpd,atftpd,rpcbind}; rm /etc/ethers
# ===========================================================================
set -eu

D=/home/dolbeau2/Sun3_BootDir

setcap cap_net_raw+ep          "$D/sbin/rarpd"
setcap cap_net_bind_service+ep "$D/sbin/atftpd"
setcap cap_net_bind_service+ep "$D/sbin/rpcbind"

ln -sfn "$D/etc/ethers" /etc/ethers

echo "granted:"; getcap "$D/sbin/rarpd" "$D/sbin/atftpd" "$D/sbin/rpcbind"
echo "linked:";  ls -l /etc/ethers
