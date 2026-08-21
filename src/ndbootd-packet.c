/* ndbootd-packet.c - Linux AF_PACKET raw interface for ndbootd.
 *
 * A drop-in replacement for upstream's config/ndbootd-bpf.c, which talks to
 * /dev/bpf and so builds only on the BSDs.  ndbootd's README anticipates this:
 * "the raw interface support is broken out, which should allow for reasonable"
 * porting.  The three functions below are the entire contract, declared in
 * ndbootd.h:
 *
 *   ndbootd_raw_open   bind a link-layer socket to one interface, filtered
 *                      down to ND requests
 *   ndbootd_raw_read   return one complete Ethernet frame, blocking
 *   ndbootd_raw_write  send one complete Ethernet frame, headers and all
 *
 * Link-layer access is not an optimisation here: a Sun-2 has no IP address
 * until it reads one out of our first reply, so there is no address to send
 * to.  We answer its Ethernet address directly.
 *
 * Requires CAP_NET_RAW, which is the same capability rarpd already carries.
 *
 * Written for Sun3_BootDir; same licence terms as ndbootd itself (see
 * COPYING in the upstream distribution).
 */

#include <poll.h>
#include <net/if.h>
#include <netpacket/packet.h>
#include <linux/filter.h>

/* Linux 4.20 and later can drop our own transmissions in the kernel.  Older
   kernels fall back to the PACKET_OUTGOING test in ndbootd_raw_read. */
#ifndef PACKET_IGNORE_OUTGOING
#define PACKET_IGNORE_OUTGOING 23
#endif

struct _ndbootd_interface_packet {
	int _ndbootd_interface_packet_ifindex;
};

/* The same filter upstream gives BPF, in Linux's struct sock_filter -- the
   classic BPF instruction encoding is identical, and an AF_PACKET SOCK_RAW
   socket presents the frame starting at the Ethernet header just as BPF does,
   so the offsets carry over unchanged.
   Accept a frame only if it is IP, protocol ND, not a fragment, and carries no
   data beyond the ND header (that is, it is a request rather than a reply). */
static struct sock_filter ndboot_packet_filter[] = {

	/* drop this packet if its ethertype isn't ETHERTYPE_IP: */
	BPF_STMT(BPF_LD + BPF_H + BPF_ABS, NDBOOTD_OFFSETOF(struct ether_header, ether_type)),
	BPF_JUMP(BPF_JMP + BPF_JEQ + BPF_K, ETHERTYPE_IP, 0, 9),

	/* drop this packet if its IP protocol isn't IPPROTO_ND: */
	BPF_STMT(BPF_LD + BPF_B + BPF_ABS, sizeof(struct ether_header) + NDBOOTD_OFFSETOF(struct ip, ip_p)),
	BPF_JUMP(BPF_JMP + BPF_JEQ + BPF_K, IPPROTO_ND, 0, 7),

	/* drop this packet if it's a fragment: */
	BPF_STMT(BPF_LD + BPF_H + BPF_ABS, sizeof(struct ether_header) + NDBOOTD_OFFSETOF(struct ip, ip_off)),
	BPF_JUMP(BPF_JMP + BPF_JSET + BPF_K, 0x3fff, 5, 0),

	/* drop this packet if it is carrying data (we only want requests,
	 * which have no data): */
	BPF_STMT(BPF_LD + BPF_H + BPF_ABS, sizeof(struct ether_header) + NDBOOTD_OFFSETOF(struct ip, ip_len)),
	BPF_STMT(BPF_LDX + BPF_B + BPF_MSH, sizeof(struct ether_header)),
	BPF_STMT(BPF_ALU + BPF_SUB + BPF_X, 0),
	BPF_JUMP(BPF_JMP + BPF_JEQ + BPF_K, sizeof(struct ndboot_packet), 0, 1),

	/* accept this packet: */
	BPF_STMT(BPF_RET + BPF_K, (u_int) -1),

	/* drop this packet: */
	BPF_STMT(BPF_RET + BPF_K, 0),
};

/* this opens a raw socket using AF_PACKET. */
int
ndbootd_raw_open(struct ndbootd_interface * interface)
{
	int network_fd;
	int saved_errno;
	int opt;
	unsigned int ifindex;
	struct sockaddr_ll sll;
	struct sock_fprog program;
	struct _ndbootd_interface_packet *interface_packet;
	const char *ifname = interface->ndbootd_interface_ifreq->ifr_name;

	ifindex = if_nametoindex(ifname);
	if (ifindex == 0) {
		_NDBOOTD_DEBUG((fp, "packet: no such interface %s: %s", ifname, strerror(errno)));
		return (-1);
	}

	/* ETH_P_IP rather than ETH_P_ALL: the filter wants IP anyway, and this
	   keeps every other frame on the wire out of the socket entirely. */
	network_fd = socket(AF_PACKET, SOCK_RAW, htons(ETHERTYPE_IP));
	if (network_fd < 0) {
		_NDBOOTD_DEBUG((fp, "packet: failed to open an AF_PACKET socket: %s", strerror(errno)));
		if (errno == EPERM || errno == EACCES) {
			_NDBOOTD_DEBUG((fp, "packet: an AF_PACKET socket needs CAP_NET_RAW"));
		}
		return (-1);
	}
	_NDBOOTD_DEBUG((fp, "packet: opened an AF_PACKET socket for %s (index %u)", ifname, ifindex));

#define _NDBOOTD_RAW_OPEN_ERROR(x) saved_errno = errno; x; errno = saved_errno

	/* bind to the one interface, so we never see another segment's ND: */
	memset(&sll, 0, sizeof(sll));
	sll.sll_family = AF_PACKET;
	sll.sll_protocol = htons(ETHERTYPE_IP);
	sll.sll_ifindex = (int) ifindex;
	if (bind(network_fd, (struct sockaddr *) &sll, sizeof(sll)) < 0) {
		_NDBOOTD_DEBUG((fp, "packet: failed to bind to %s: %s", ifname, strerror(errno)));
		_NDBOOTD_RAW_OPEN_ERROR(close(network_fd));
		return (-1);
	}

	/* filter down to ND requests: */
	memset(&program, 0, sizeof(program));
	program.len = sizeof(ndboot_packet_filter) / sizeof(ndboot_packet_filter[0]);
	program.filter = ndboot_packet_filter;
	if (setsockopt(network_fd, SOL_SOCKET, SO_ATTACH_FILTER, &program, sizeof(program)) < 0) {
		_NDBOOTD_DEBUG((fp, "packet: failed to set the filter on %s: %s", ifname, strerror(errno)));
		_NDBOOTD_RAW_OPEN_ERROR(close(network_fd));
		return (-1);
	}

	/* Our own replies come back to us on a packet socket.  Ask the kernel
	   not to bother; if it is too old to know the option, ndbootd_raw_read
	   discards them instead, so this failing is not an error. */
	opt = 1;
	if (setsockopt(network_fd, SOL_PACKET, PACKET_IGNORE_OUTGOING, &opt, sizeof(opt)) < 0) {
		_NDBOOTD_DEBUG((fp, "packet: PACKET_IGNORE_OUTGOING unavailable (%s), filtering in userland",
			strerror(errno)));
	}

	interface->ndbootd_interface_fd = network_fd;
	interface_packet = ndbootd_new0(struct _ndbootd_interface_packet, 1);
	interface_packet->_ndbootd_interface_packet_ifindex = (int) ifindex;
	interface->_ndbootd_interface_raw_private = interface_packet;
	return (0);
#undef _NDBOOTD_RAW_OPEN_ERROR
}

/* this reads a raw packet: */
int
ndbootd_raw_read(struct ndbootd_interface * interface, void *packet_buffer, size_t packet_buffer_size)
{
	struct _ndbootd_interface_packet *interface_packet;
	struct sockaddr_ll from;
	socklen_t from_len;
	ssize_t packet_size;
	struct pollfd set[1];

	interface_packet = (struct _ndbootd_interface_packet *) interface->_ndbootd_interface_raw_private;

	/* A packet socket hands back one frame per recvfrom, so there is no
	   buffer to unpack here -- the whole of upstream's BPF header walk
	   collapses into this loop. */
	set[0].fd = interface->ndbootd_interface_fd;
	set[0].events = POLLIN;
	for (;;) {

		_NDBOOTD_DEBUG((fp, "packet: calling poll"));
		switch (poll(set, 1, -1)) {
		case 0:
			_NDBOOTD_DEBUG((fp, "packet: poll returned zero"));
			continue;
		case 1:
			break;
		default:
			if (errno == EINTR) {
				_NDBOOTD_DEBUG((fp, "packet: poll got EINTR"));
				continue;
			}
			_NDBOOTD_DEBUG((fp, "packet: poll failed: %s", strerror(errno)));
			return (-1);
		}

		from_len = sizeof(from);
		memset(&from, 0, sizeof(from));
		packet_size = recvfrom(interface->ndbootd_interface_fd,
		    packet_buffer, packet_buffer_size, 0,
		    (struct sockaddr *) &from, &from_len);
		if (packet_size < 0) {
			if (errno == EINTR || errno == EAGAIN) {
				continue;
			}
			_NDBOOTD_DEBUG((fp, "packet: failed to read a packet: %s", strerror(errno)));
			return (-1);
		}
		_NDBOOTD_DEBUG((fp, "packet: read a %ld byte frame", (long) packet_size));

		/* ignore anything that arrived on another interface: */
		if (from.sll_ifindex != interface_packet->_ndbootd_interface_packet_ifindex) {
			continue;
		}

		/* silently ignore packets that don't even have Ethernet
		 * headers, and those packets that we transmitted: */
		if ((size_t) packet_size < sizeof(struct ether_header)
		    || from.sll_pkttype == PACKET_OUTGOING
		    || !memcmp(((struct ether_header *) packet_buffer)->ether_shost,
			interface->ndbootd_interface_ether,
			ETHER_ADDR_LEN)) {
			continue;
		}

		return ((int) packet_size);
	}
	/* NOTREACHED */
}

/* this writes a raw packet: */
int
ndbootd_raw_write(struct ndbootd_interface * interface, void *packet_buffer, size_t packet_buffer_size)
{
	struct _ndbootd_interface_packet *interface_packet;
	struct sockaddr_ll sll;

	interface_packet = (struct _ndbootd_interface_packet *) interface->_ndbootd_interface_raw_private;

	/* The frame already carries its Ethernet header -- ndbootd built the
	   reply by swapping the request's addresses -- but AF_PACKET still
	   wants to be told which interface to put it on. */
	if (packet_buffer_size < sizeof(struct ether_header)) {
		errno = EINVAL;
		return (-1);
	}
	memset(&sll, 0, sizeof(sll));
	sll.sll_family = AF_PACKET;
	sll.sll_protocol = htons(ETHERTYPE_IP);
	sll.sll_ifindex = interface_packet->_ndbootd_interface_packet_ifindex;
	sll.sll_halen = ETHER_ADDR_LEN;
	memcpy(sll.sll_addr, ((struct ether_header *) packet_buffer)->ether_dhost, ETHER_ADDR_LEN);

	return ((int) sendto(interface->ndbootd_interface_fd,
		packet_buffer, packet_buffer_size, 0,
		(struct sockaddr *) &sll, sizeof(sll)));
}
