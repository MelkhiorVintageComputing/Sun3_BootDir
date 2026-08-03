#!/usr/bin/env python3
"""Reproduce the bootparams exchange the Sun-3 makes, from this host.

This is the most fragile link in the chain, and the one you cannot see failing
from the PROM (netboot just sits in "Requesting boot parameters").  netboot does
not look bootparamd up by name: it sends a PMAPPROC_CALLIT to the *broadcast*
address on port 111 with a BOOTPARAMPROC_WHOAMI request wrapped inside, and
whichever portmapper forwards it identifies itself as the bootparams server.
See sys/lib/libsa/bootparam.c in the NetBSD tree.

That means this probe fails, exactly as the Sun-3 would, if:
  * rpcbind is missing -r          -- indirect calls are dropped silently
  * rpcbind is missing -i          -- bootparamd could not register
  * rpcbind was given -h           -- it never sees the broadcast
  * bootparamd cannot resolve the client's IP to a name

Usage:
    bp-probe.py [--server ADDR] [--broadcast] [CLIENT_IP]
"""

import argparse
import os
import socket
import struct
import sys

import sun3conf

PMAPPROG, PMAPVERS, PMAPPROC_CALLIT = 100000, 2, 5
BOOTPARAMPROG, BOOTPARAMVERS = 100026, 1
BOOTPARAMPROC_WHOAMI, BOOTPARAMPROC_GETFILE = 1, 2

AUTH_NULL, AUTH_UNIX = 0, 1
MSG_ACCEPTED, SUCCESS = 0, 0


def xdr_string(s):
    b = s.encode()
    return struct.pack("!I", len(b)) + b + b"\0" * (-len(b) % 4)


def xdr_inaddr(ip):
    """bp_address: an int discriminant then one int per octet.  Four ints,
    not four chars -- the comment in bootparam.c calls this 'Blech.'"""
    octets = [int(x) for x in ip.split(".")]
    return struct.pack("!I", 1) + struct.pack("!4i", *octets)


class Decoder:
    def __init__(self, buf):
        self.buf, self.off = buf, 0

    def u32(self):
        v, = struct.unpack_from("!I", self.buf, self.off)
        self.off += 4
        return v

    def string(self):
        n = self.u32()
        s = self.buf[self.off:self.off + n]
        self.off += n + (-n % 4)
        return s.decode(errors="replace")

    def inaddr(self):
        self.u32()  # discriminant
        octets = struct.unpack_from("!4i", self.buf, self.off)
        self.off += 16
        return ".".join(str(o & 0xFF) for o in octets)


def auth_unix():
    host = socket.gethostname()[:255]
    body = (struct.pack("!I", 0) + xdr_string(host)
            + struct.pack("!III", os.getuid(), os.getgid(), 0))
    return struct.pack("!II", AUTH_UNIX, len(body)) + body


def rpc_call(sock, addr, prog, vers, proc, args, timeout=5.0, cred=None):
    xid = struct.unpack("!I", os.urandom(4))[0]
    header = struct.pack("!IIIIII", xid, 0, 2, prog, vers, proc)
    cred = cred if cred is not None else struct.pack("!II", AUTH_NULL, 0)
    verf = struct.pack("!II", AUTH_NULL, 0)
    sock.settimeout(timeout)
    sock.sendto(header + cred + verf + args, addr)

    while True:
        try:
            reply, frm = sock.recvfrom(65536)
        except socket.timeout:
            raise TimeoutError(f"no reply from {addr[0]}:{addr[1]}")
        if len(reply) < 24:
            continue
        d = Decoder(reply)
        if d.u32() != xid:
            continue
        if d.u32() != 1:            # REPLY
            continue
        if d.u32() != MSG_ACCEPTED:
            raise RuntimeError("RPC call rejected (auth or version mismatch)")
        d.u32()                     # verifier flavour
        verf_len = d.u32()          # ... and its body, which we skip
        d.off += verf_len
        status = d.u32()
        if status != SUCCESS:
            names = {1: "PROG_UNAVAIL", 2: "PROG_MISMATCH", 3: "PROC_UNAVAIL",
                     4: "GARBAGE_ARGS", 5: "SYSTEM_ERR"}
            raise RuntimeError(f"RPC accept_stat={names.get(status, status)}")
        return reply[d.off:], frm


def whoami(sock, server, client_ip):
    inner = xdr_inaddr(client_ip)
    args = (struct.pack("!IIII", BOOTPARAMPROG, BOOTPARAMVERS,
                        BOOTPARAMPROC_WHOAMI, len(inner)) + inner)
    body, frm = rpc_call(sock, (server, 111), PMAPPROG, PMAPVERS,
                         PMAPPROC_CALLIT, args)
    d = Decoder(body)
    port = d.u32()
    encap_len = d.u32()
    if encap_len == 0:
        raise RuntimeError("portmapper forwarded nothing back")
    inner = Decoder(body[d.off:d.off + encap_len])
    return {
        "responder": frm[0],
        "bootparamd_port": port,
        "client_name": inner.string(),
        "domain_name": inner.string(),
        "router": inner.inaddr(),
    }


def getfile(sock, server, port, client_name, file_id):
    args = xdr_string(client_name) + xdr_string(file_id)
    body, _ = rpc_call(sock, (server, port), BOOTPARAMPROG, BOOTPARAMVERS,
                       BOOTPARAMPROC_GETFILE, args, cred=auth_unix())
    d = Decoder(body)
    return {
        "server_name": d.string(),
        "server_addr": d.inaddr(),
        "server_path": d.string(),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("client_ip", nargs="?", default=None,
                    help="the Sun-3's IP (default: the --client entry's)")
    ap.add_argument("--client", default=None,
                    help="which CLIENTS entry to use, by name or IP "
                         "(default: the first)")
    ap.add_argument("--server", default="127.0.0.1",
                    help="portmapper to ask (default 127.0.0.1)")
    ap.add_argument("--broadcast", action="store_true",
                    help="send to 255.255.255.255 like the real client does")
    args = ap.parse_args()

    client_ip = args.client_ip or sun3conf.pick(args.client)["ip"]

    server = "255.255.255.255" if args.broadcast else args.server

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    print(f"WHOAMI  {client_ip} via PMAPPROC_CALLIT to {server}:111")
    try:
        w = whoami(sock, server, client_ip)
    except Exception as exc:
        print(f"  FAILED: {exc}")
        print("  check: rpcbind running with -r and -i, bootparamd registered")
        print("         (scripts/status.sh shows both), and that etc/hosts has")
        print(f"         an entry for {client_ip}")
        return 1
    print(f"  answered by   {w['responder']}")
    print(f"  bootparamd on port {w['bootparamd_port']}")
    print(f"  client_name   {w['client_name']!r}")
    print(f"  domain_name   {w['domain_name']!r}")
    print(f"  router        {w['router']}")

    # netboot asks for "gateway" before "root" and uses the address it gets
    # back as its default route.  An empty answer is not fatal to the probe --
    # a client on the server's own segment boots without one -- but it is worth
    # reporting, because a missing entry is invisible until something needs to
    # be routed.
    print(f"\nGETFILE gateway for {w['client_name']!r}")
    try:
        gw = getfile(sock, w["responder"], w["bootparamd_port"],
                     w["client_name"], "gateway")
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 1
    if gw["server_name"]:
        print(f"  server_name   {gw['server_name']!r}")
        print(f"  server_addr   {gw['server_addr']}")
    else:
        print("  no answer -- etc/bootparams has no 'gateway=' for this client,")
        print("  so it will boot without a default route")

    print(f"\nGETFILE root for {w['client_name']!r}")
    try:
        g = getfile(sock, w["responder"], w["bootparamd_port"],
                    w["client_name"], "root")
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 1
    print(f"  server_name   {g['server_name']!r}")
    print(f"  server_addr   {g['server_addr']}")
    print(f"  server_path   {g['server_path']!r}")

    if not g["server_path"]:
        print("\n  empty root path -- etc/bootparams has no 'root=' for this client")
        return 1
    print("\nOK: netboot would now NFS-mount "
          f"{g['server_addr']}:{g['server_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
