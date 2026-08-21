#!/bin/sh
# ===========================================================================
# The ONLY part of the Sun-3 netboot environment that needs root.
# Review it, run it once as root, and you are done.  Everything else in
# /home/dolbeau2/Sun3_BootDir runs as an ordinary user.
#
# Four daemons need one capability each; nothing runs as root afterwards:
#   rarpd    RARP replies come from an AF_PACKET socket        -> CAP_NET_RAW
#   ndbootd  a Sun-2 has no IP address until we reply to its
#            Ethernet address, so ND is served link-layer too  -> CAP_NET_RAW
#   atftpd   TFTP listens on udp/69                            -> CAP_NET_BIND_SERVICE
#   rpcbind  the portmapper listens on udp+tcp/111             -> CAP_NET_BIND_SERVICE
#
# ndbootd is only needed if you have a Sun-2; the line is harmless otherwise,
# but drop it if sbin/ndbootd does not exist.
#
# The symlink exists because neither rarpd nor ndbootd has an option to
# relocate the MAC->IP table they share; pointing /etc/ethers into the boot
# directory keeps that table editable without root, so this script never has
# to be run again when a client's address changes.
#
# Capabilities live on the inode, so any of these binaries that gets rebuilt
# loses its capability and this script must be run again.  The build scripts
# say so when it happens.
#
# To undo everything:
#   setcap -r $D/sbin/{rarpd,ndbootd,atftpd,rpcbind}; rm /etc/ethers
# ===========================================================================
set -eu

D=/home/dolbeau2/Sun3_BootDir

setcap cap_net_raw+ep          "$D/sbin/rarpd"
setcap cap_net_bind_service+ep "$D/sbin/atftpd"
setcap cap_net_bind_service+ep "$D/sbin/rpcbind"
if [ -x "$D/sbin/ndbootd" ]; then
	setcap cap_net_raw+ep  "$D/sbin/ndbootd"
fi

ln -sfn "$D/etc/ethers" /etc/ethers

echo "granted:"; getcap "$D"/sbin/* 2>/dev/null || true
echo "linked:";  ls -l /etc/ethers
