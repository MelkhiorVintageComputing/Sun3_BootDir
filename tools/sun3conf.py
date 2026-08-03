"""Read config/sun3boot.conf the way the shell scripts do.

The probes each used to scrape the config themselves.  Once CLIENT_* became a
CLIENTS table that stopped being a one-liner, so it lives here instead and all
three share it.

This is not a shell parser.  It handles plain VAR=value and the one quoted
multi-line assignment the config actually contains, which is what the config's
own header promises is all you will find in it.
"""

import os
import re

BOOTDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(BOOTDIR, "config", "sun3boot.conf")


def read(path=CONF):
    """Return {name: value} for every assignment in the config."""
    with open(path) as fh:
        text = fh.read()
    conf = {}
    for m in re.finditer(
        r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)=" r"(?:'([^']*)'|\"([^\"]*)\"|([^\n#]*))",
        text,
        re.MULTILINE,
    ):
        name = m.group(1)
        value = next(g for g in m.groups()[1:] if g is not None)
        conf[name] = value.strip()
    return conf


def clients(conf=None):
    """The client table as a list of dicts, in config order.

    Mirrors clients() in scripts/common.sh, including the fallback to a
    pre-table config that names a single CLIENT_*.
    """
    conf = read() if conf is None else conf
    out = []
    for line in conf.get("CLIENTS", "").splitlines():
        line = line.split("#")[0].split()
        if not line:
            continue
        if len(line) < 3:
            raise ValueError(f"malformed CLIENTS line: {' '.join(line)!r}")
        name, mac, ip = line[0], line[1], line[2]
        arch = line[3] if len(line) > 3 else "sun3"
        out.append({"name": name, "mac": mac, "ip": ip, "arch": arch})
    if not out and conf.get("CLIENT_IP"):
        out.append({
            "name": conf.get("CLIENT_NAME", "sun3"),
            "mac": conf.get("CLIENT_MAC", ""),
            "ip": conf["CLIENT_IP"],
            "arch": conf.get("CLIENT_ARCH", "sun3"),
        })
    if not out:
        raise ValueError(f"no clients defined in {CONF}")
    return out


def pick(name=None):
    """One client: the named one, or the first if no name is given."""
    all_ = clients()
    if name is None:
        return all_[0]
    for c in all_:
        if c["name"] == name or c["ip"] == name:
            return c
    known = ", ".join(f"{c['name']} ({c['ip']})" for c in all_)
    raise SystemExit(f"no such client: {name}.  Known clients: {known}")


def tftpname(client):
    """The filename the PROM asks for: the IP in uppercase hex, plus the
    architecture suffix on a sun3x."""
    octets = [int(x) for x in client["ip"].split(".")]
    if len(octets) != 4 or any(not 0 <= o <= 255 for o in octets):
        raise ValueError(f"not a dotted-quad IPv4 address: {client['ip']}")
    name = "%02X%02X%02X%02X" % tuple(octets)
    return name + ".SUN3X" if client["arch"] == "sun3x" else name
