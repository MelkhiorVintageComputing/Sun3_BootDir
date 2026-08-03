#!/usr/bin/env python3
"""Request a file over TFTP the way a Sun-3 PROM does: by broadcasting.

The Sun-3 does not learn the server's IP address from RARP, so it sends its
TFTP read request to 255.255.255.255 and accepts the reply from whoever answers.
That matters, because a TFTP server that answers such a request from the wrong
source address produces a transfer that times out on both ends while looking,
in the logs, as though it started normally:

    Creating new socket: 255.255.255.255:58938
    Serving C0A80079 to 192.168.0.121:1756
    timeout block 0: retrying ...
    client (192.168.0.121) not responding

An ordinary TFTP client sends its request to the server's unicast address and
never reproduces this.  This one does.

Usage:
    tftp-bcast-probe.py [FILENAME] [--dest ADDR] [--blocks N]
"""

import argparse
import socket
import struct
import sys

import sun3conf

RRQ, DATA, ERROR, ACK = 1, 3, 5, 4


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("filename", nargs="?", default=None,
                    help="default: the name derived from the --client entry")
    ap.add_argument("--client", default=None,
                    help="which CLIENTS entry to use, by name or IP "
                         "(default: the first)")
    ap.add_argument("--dest", default="255.255.255.255",
                    help="where to send the request (default: broadcast, like the PROM)")
    ap.add_argument("--blocks", type=int, default=3,
                    help="how many 512-byte blocks to pull before giving up")
    ap.add_argument("--port", type=int, default=69,
                    help="server port (default 69; useful for testing a build "
                         "that has no capability yet)")
    args = ap.parse_args()

    filename = args.filename or sun3conf.tftpname(sun3conf.pick(args.client))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(5.0)

    # A bare RRQ with no RFC 2347 options, which is all a 1986 PROM sends.
    rrq = struct.pack("!H", RRQ) + filename.encode() + b"\0" + b"octet\0"
    print(f"RRQ {filename!r} -> {args.dest}:{args.port}")
    sock.sendto(rrq, (args.dest, args.port))

    received = 0
    for expect in range(1, args.blocks + 1):
        try:
            pkt, frm = sock.recvfrom(4096)
        except socket.timeout:
            if expect == 1:
                print("  no DATA came back within 5s.")
                print("  The server saw the request (check log/atftpd.log) but its")
                print("  reply never arrived -- that is the broadcast source-address")
                print("  bug.  See the TFTP section of README.md.")
            else:
                print(f"  transfer stalled after block {expect - 1}")
            return 1

        opcode, block = struct.unpack("!HH", pkt[:4])
        if opcode == ERROR:
            msg = pkt[4:].split(b"\0")[0].decode(errors="replace")
            print(f"  ERROR {block}: {msg}")
            return 1
        if opcode != DATA:
            print(f"  unexpected opcode {opcode} from {frm[0]}:{frm[1]}")
            return 1

        payload = pkt[4:]
        received += len(payload)
        print(f"  DATA block {block} from {frm[0]}:{frm[1]}, {len(payload)} bytes"
              + ("  <- source address is correct" if expect == 1 else ""))
        sock.sendto(struct.pack("!HH", ACK, block), frm)
        if len(payload) < 512:
            break

    print(f"\nOK: {received} bytes transferred. A broadcast RRQ is answered correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
