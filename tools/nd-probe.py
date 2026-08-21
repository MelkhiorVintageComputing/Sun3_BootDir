#!/usr/bin/env python3
"""Send a Sun ND read request the way a Sun-2 PROM does, and report the reply.

A Sun-2 has no IP address when it starts: it broadcasts an ND read request
with a zero destination address and takes its own address out of the first
reply.  This reproduces exactly that, so ndbootd can be tested without a
Sun-2 -- and, more to the point, so the Linux AF_PACKET backend in
src/ndbootd-packet.c can be shown to receive and transmit.

Needs CAP_NET_RAW, or to be run inside a user+network namespace where we are
root (which is what scripts/dryrun.sh does).

  nd-probe.py --interface veth1 --client-mac 02:00:00:00:00:02
"""

import argparse
import socket
import struct
import sys
import time

ETH_P_IP = 0x0800
IPPROTO_ND = 77

NDBOOT_OP_READ = 0x01
NDBOOT_OP_ERROR = 0x03
NDBOOT_OP_MASK = 0x07
NDBOOT_OP_FLAG_WAIT = 1 << 3
NDBOOT_OP_FLAG_DONE = 1 << 4
NDBOOT_MINOR_NDP0 = 0x40

# struct ndboot_packet: four bytes then six network-order int32s.
ND_PACKET = "!BBbb6i"
ND_PACKET_LEN = struct.calcsize(ND_PACKET)
assert ND_PACKET_LEN == 28, ND_PACKET_LEN


def mac_bytes(text):
    parts = text.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError(f"not an Ethernet address: {text}")
    return bytes(int(p, 16) for p in parts)


def ip_checksum(header):
    if len(header) % 2:
        header += b"\0"
    total = 0
    for i in range(0, len(header), 2):
        total += (header[i] << 8) + header[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def build_request(src_mac, dst_mac, src_ip, dst_ip, block, byte_count, sequence):
    nd = struct.pack(ND_PACKET,
                     NDBOOT_OP_READ,        # op
                     NDBOOT_MINOR_NDP0,     # minor: /dev/ndp0
                     0,                     # error
                     0,                     # disk version
                     sequence,
                     block,
                     byte_count,
                     0,                     # residual byte count
                     0,                     # current byte offset
                     0)                     # current byte count

    total = 20 + len(nd)
    # ttl 4 is what the PROM uses; it is not why anything works, but it makes
    # the probe look like the real thing in a capture.
    ip = struct.pack("!BBHHHBBH4s4s",
                     0x45, 0, total, 0, 0, 4, IPPROTO_ND, 0,
                     socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
    ip = ip[:10] + struct.pack("!H", ip_checksum(ip)) + ip[12:]

    ether = dst_mac + src_mac + struct.pack("!H", ETH_P_IP)
    return ether + ip + nd


def describe(op):
    base = op & NDBOOT_OP_MASK
    name = {NDBOOT_OP_READ: "READ", NDBOOT_OP_ERROR: "ERROR"}.get(base, f"op {base}")
    flags = []
    if op & NDBOOT_OP_FLAG_WAIT:
        flags.append("WAIT")
    if op & NDBOOT_OP_FLAG_DONE:
        flags.append("DONE")
    return name + ("|" + "|".join(flags) if flags else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interface", required=True,
                    help="interface to send from and listen on")
    ap.add_argument("--client-mac", default=None,
                    help="pretend to be this Ethernet address "
                         "(default: the interface's own)")
    ap.add_argument("--block", type=int, default=0,
                    help="ND block number to read (default 0)")
    ap.add_argument("--byte-count", type=int, default=512,
                    help="how many bytes to ask for (default 512)")
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args()

    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
    except PermissionError:
        print("an AF_PACKET socket needs CAP_NET_RAW", file=sys.stderr)
        return 1
    sock.bind((args.interface, ETH_P_IP))
    sock.settimeout(args.timeout)

    own_mac = sock.getsockname()[4]
    src_mac = mac_bytes(args.client_mac) if args.client_mac else own_mac
    pretty = ":".join(f"{b:02x}" for b in src_mac)

    # A Sun-2 knows neither address, so both are zero and the frame is a
    # broadcast.  ndbootd recognises the zero destination and fills in the
    # client's address from /etc/ethers.
    request = build_request(src_mac, b"\xff" * 6, "0.0.0.0", "0.0.0.0",
                            args.block, args.byte_count, 0)
    print(f"ND READ block {args.block}, {args.byte_count} bytes, as {pretty}")
    print(f"  -> broadcast on {args.interface}, {len(request)} byte frame")
    sock.send(request)

    deadline = time.monotonic() + args.timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("  no reply: nothing answered the ND request", file=sys.stderr)
            return 1
        sock.settimeout(remaining)
        try:
            frame = sock.recv(65536)
        except socket.timeout:
            print("  no reply: nothing answered the ND request", file=sys.stderr)
            return 1

        if len(frame) < 14 + 20 + ND_PACKET_LEN:
            continue
        if frame[6:12] == src_mac:          # our own transmission
            continue
        if frame[12:14] != struct.pack("!H", ETH_P_IP):
            continue
        ihl = (frame[14] & 0x0F) * 4
        if frame[14 + 9] != IPPROTO_ND:
            continue

        src_ip = socket.inet_ntoa(frame[14 + 12:14 + 16])
        dst_ip = socket.inet_ntoa(frame[14 + 16:14 + 20])
        nd = struct.unpack(ND_PACKET, frame[14 + ihl:14 + ihl + ND_PACKET_LEN])
        op, minor, error, version, seq, block, bcount, resid, off, count = nd
        data = frame[14 + ihl + ND_PACKET_LEN:]

        print(f"  <- {describe(op)} from {src_ip} to {dst_ip}")
        print(f"     block {block}  byte_count {bcount}  residual {resid}")
        print(f"     offset {off}  count {count}  error {error}")
        if data:
            print(f"     {len(data)} bytes of data, first 16: {data[:16].hex()}")

        if (op & NDBOOT_OP_MASK) == NDBOOT_OP_ERROR:
            print(f"\n  ndbootd returned an ND error ({error})", file=sys.stderr)
            return 1
        if not data:
            print("\n  reply carried no data", file=sys.stderr)
            return 1

        print(f"\nOK: ndbootd answered, and told us our address is {dst_ip}.")
        print("    That is how a Sun-2 learns its own IP.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
