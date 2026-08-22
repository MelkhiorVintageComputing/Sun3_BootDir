#!/bin/sh
# ===========================================================================
# The second, and last, thing here that needs root.  Only a Sun-2 running
# SunOS needs it; a NetBSD client of any kind does not.
#
# SunOS 4.0.3's /boot has its own IP address from RARP but no netmask, so
# when it broadcasts its bootparams request it uses the 4.2BSD form: the
# network address with the host part all zeros.  Seen on the wire:
#
#     192.168.0.123.1023 > 192.168.0.0.111: UDP, length 100
#
# Modern Linux installs a broadcast route for 192.168.0.255 and none for
# 192.168.0.0, so that datagram is dropped in the input path and no daemon
# ever sees it.  The Sun-2 then reports
#
#     Boot: bad dialog with bootparam server (error 0x4)
#
# which is "RPC: Unable to receive" -- it sent and heard nothing back.
#
# Adding the address makes the kernel accept that destination locally, and
# rpcbind is already listening on 0.0.0.0:111.  It changes nothing else:
# the /24 keeps its own source address, so outgoing traffic still uses
# 192.168.0.31.
#
# These values match SERVER_IF and the clients' subnet in
# config/sun3boot.conf.  Change them together.
#
# This does NOT survive a reboot.  scripts/selftest.sh and scripts/status.sh
# both check for it and say so when a sunos client is configured.
#
# To undo:  ip addr del 192.168.0.0/32 dev eno1
# ===========================================================================
set -eu

IF=eno1
NET=192.168.0.0

ip addr replace "$NET/32" dev "$IF"

echo "added:"; ip -4 -o addr show dev "$IF"
echo "local table:"; ip -4 route show table local | grep " $NET "
