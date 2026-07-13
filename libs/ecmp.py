#!/usr/bin/env python3
"""Helpers for the ECMP source-IP hashing test-suite.

Routing on the DUT is driven through **FRR** (``vtysh``): static routes go via
``staticd``, and the BGP/OSPF scenarios are described by ready-made ``frr.conf``
files under ``tests/infra/<scenario>/``.  The kernel FIB / multipath hashing
(``fib_multipath_hash_*``) is what the product's ``ip load-sharing`` knobs map
onto.

The ``networkop/cx`` image places the first interface in a Cumulus ``mgmt`` VRF
and expects ``/etc/frr/*`` to be owned by ``frr`` - both are handled here.
"""

import json
import time


HASH_MODES = {
    # product mode -> (fib_multipath_hash_policy, fib_multipath_hash_fields)
    "source-ip":   (3, 0x0001),
    "dst-ip":      (3, 0x0002),
    "src-dst-ip":  (0, None),
    "l3":          (0, None),
    "5-tuple":     (1, None),
    "l4":          (1, None),
}


class Node:
    """A container we can run commands in."""

    def __init__(self, env, name):
        self.env = env
        self.name = name

    def exec(self, cmd, check=False):
        code, out = self.env.exec_command(self.name, cmd)
        if check and code != 0:
            raise RuntimeError(f"[{self.name}] `{cmd}` -> {code}\n{out}")
        return code, out

    def sh(self, cmd, check=True):
        return self.exec(cmd, check=check)[1]

    # -- interface / address helpers ------------------------------------

    def iface_for_ip(self, ip):
        for line in self.sh("ip -o -4 addr show", check=False).splitlines():
            f = line.split()
            if len(f) >= 4 and f[3].split("/")[0] == ip:
                return f[1]
        return None

    def mac_of(self, iface):
        return self.sh(f"cat /sys/class/net/{iface}/address").strip()

    def mac_for_ip(self, ip):
        return self.mac_of(self.iface_for_ip(ip))

    # -- Cumulus mgmt-VRF / IPv6 --------------------------------------

    def leave_mgmt_vrf(self):
        """Move every interface enslaved to the Cumulus ``mgmt`` VRF back into
        the default VRF, preserving its addresses.  systemd may enslave an
        interface late during boot, so this retries until it stays clean."""
        script = (
            "import json,subprocess,time\n"
            "def sh(c):return subprocess.run(c,shell=True,capture_output=True,text=True).stdout\n"
            "clean=0\n"
            "for _ in range(20):\n"
            "    moved=False\n"
            "    for l in json.loads(sh('ip -j link show')):\n"
            "        if l.get('master')=='mgmt':\n"
            "            i=l['ifname']\n"
            "            a4=[a['local']+'/'+str(a['prefixlen']) for a in (json.loads(sh('ip -j -4 addr show dev '+i))[0].get('addr_info') or [])]\n"
            "            a6=[a['local']+'/'+str(a['prefixlen']) for a in (json.loads(sh('ip -j -6 addr show dev '+i))[0].get('addr_info') or []) if a.get('scope')=='global']\n"
            "            subprocess.run('ip link set '+i+' nomaster',shell=True)\n"
            "            for a in a4+a6: subprocess.run('ip addr add '+a+' dev '+i,shell=True)\n"
            "            subprocess.run('ip link set '+i+' up',shell=True)\n"
            "            moved=True\n"
            "    clean = 0 if moved else clean+1\n"
            "    if clean>=3: break\n"
            "    time.sleep(1)\n"
        )
        self.exec(["python3", "-c", script])

    def enable_ipv6(self):
        self.exec("sh -c 'for f in /proc/sys/net/ipv6/conf/*/disable_ipv6; "
                  "do echo 0 > $f; done'")

    def disable_offloading(self):
        self.exec("for d in $(ls /sys/class/net/ | grep -v lo); do "
                  "ethtool -K $d tx off rx off 2>/dev/null; "
                  "done; true")

    def add_addr(self, addr, dev, family="ipv4"):
        ipcmd = "ip -6" if family == "ipv6" else "ip"
        self.sh(f"{ipcmd} addr add {addr} dev {dev}", check=False)


class FrrNode(Node):
    """A container running FRR."""

    def install_frr_config(self):
        """Copy the read-only mounted config into /etc/frr and fix ownership
        (FRR refuses configs not owned by frr:frr; the source is bind-mounted
        read-only so we must copy rather than chown in place)."""
        self.exec("sh -c 'mkdir -p /etc/frr; "
                  "cp -f /cfg/daemons /etc/frr/daemons 2>/dev/null; "
                  "cp -f /cfg/frr.conf /etc/frr/frr.conf 2>/dev/null; "
                  "chown frr:frr /etc/frr/daemons /etc/frr/frr.conf 2>/dev/null; "
                  "chmod 640 /etc/frr/daemons /etc/frr/frr.conf 2>/dev/null'")

    def start_frr(self):
        # The cx image is systemd-managed; FRR must be (re)started through
        # systemd, otherwise the boot sequence terminates a hand-started
        # instance a few seconds later.
        self.exec("systemctl restart frr")
        if not self.wait_daemons(20):
            self.exec("systemctl restart frr")
            self.wait_daemons(20)

    def vtysh(self, *lines, check=False):
        args = ["vtysh"]
        for ln in lines:
            args += ["-c", ln]
        return self.exec(args, check=check)

    def configure(self, *lines, check=False):
        return self.vtysh("configure terminal", *lines, check=check)

    def wait_daemons(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, out = self.vtysh("show version")
            if code == 0 and "FRR" in out:
                return True
            time.sleep(1)
        return False


class Dut(FrrNode):
    """Kernel/FRR routing device under test."""

    def base_setup(self):
        self.sh("sysctl -w net.ipv4.ip_forward=1", check=False)
        self.sh("sysctl -w net.ipv6.conf.all.forwarding=1", check=False)
        self.sh("sysctl -w net.ipv4.conf.all.accept_local=1", check=False)
        self.exec("sh -c 'for f in /proc/sys/net/ipv4/conf/*/rp_filter; "
                  "do echo 0 > $f; done'")
        self.exec("iptables -P FORWARD ACCEPT 2>/dev/null; "
                  "iptables -F FORWARD 2>/dev/null; "
                  "iptables -I FORWARD -j ACCEPT 2>/dev/null; true")

    # -- hashing --------------------------------------------------------

    def set_hash_mode(self, mode, family="ipv4"):
        policy, fields = HASH_MODES[mode]
        self.sh(f"sysctl -w net.{family}.fib_multipath_hash_policy={policy}")
        if fields is not None:
            self.sh(f"sysctl -w net.{family}.fib_multipath_hash_fields={fields}",
                    check=False)

    # -- routing via FRR (staticd) --------------------------------------

    @staticmethod
    def _rcmd(family):
        return "ipv6 route" if family == "ipv6" else "ip route"

    def add_ecmp_route(self, prefix, nexthops, family="ipv4"):
        # replace semantics: drop any existing entries for the prefix first
        self.del_route(prefix, family)
        lines = [f"{self._rcmd(family)} {prefix} {nh}" for nh in nexthops]
        self.configure(*lines, check=True)

    def add_route(self, prefix, nexthop, distance=None, family="ipv4"):
        line = f"{self._rcmd(family)} {prefix} {nexthop}"
        if distance is not None:
            line += f" {distance}"
        self.configure(line, check=True)

    def blackhole(self, prefix, family="ipv4"):
        self.configure(f"{self._rcmd(family)} {prefix} blackhole", check=True)

    def del_route(self, prefix, family="ipv4"):
        # remove every static entry for this prefix
        for line in self._static_lines(prefix):
            self.configure(f"no {line}")

    def clear_static_routes(self):
        lines = self._static_lines()
        if lines:
            self.configure(*[f"no {ln}" for ln in lines])

    def _static_lines(self, prefix=None):
        out = self.sh("vtysh -c 'show running-config'", check=False)
        res = []
        for ln in out.splitlines():
            s = ln.strip()
            if s.startswith("ip route ") or s.startswith("ipv6 route "):
                if prefix is None or f" {prefix} " in f" {s} ":
                    res.append(s)
        return res

    def show_route(self, prefix, family="ipv4"):
        cmd = "show ipv6 route" if family == "ipv6" else "show ip route"
        return self.vtysh(f"{cmd} {prefix}")[1]

    def fib_nexthops(self, prefix, family="ipv4"):
        """Next-hop IPs actually installed in the kernel FIB (handles nexthop
        groups)."""
        ipcmd = "ip -6 -j" if family == "ipv6" else "ip -j"
        out = self.sh(f"{ipcmd} route show {prefix}", check=False)
        try:
            routes = json.loads(out)
        except Exception:
            return []
        nhs = []
        for r in routes:
            if "gateway" in r:
                nhs.append(r["gateway"])
            for nh in r.get("nexthops", []):
                if "gateway" in nh:
                    nhs.append(nh["gateway"])
        # nexthop groups: resolve via `ip nexthop`
        if not nhs:
            for r in routes:
                if "nhid" in r:
                    nhs += self._nexthop_group(r["nhid"], family)
        return nhs

    def wait_fib(self, prefix, count, family="ipv4", timeout=55):
        """Wait until at least ``count`` next-hops are installed in the FIB."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(set(self.fib_nexthops(prefix, family))) >= count:
                return True
            time.sleep(2)
        return len(set(self.fib_nexthops(prefix, family))) >= count

    def _nexthop_group(self, nhid, family="ipv4"):
        ipcmd = "ip -6 -j" if family == "ipv6" else "ip -j"
        out = self.sh(f"{ipcmd} nexthop show id {nhid}", check=False)
        try:
            entries = json.loads(out)
        except Exception:
            return []
        nhs = []
        for e in entries:
            if "gateway" in e:
                nhs.append(e["gateway"])
            for g in e.get("group", []):
                gid = g.get("id")
                if gid is not None:
                    nhs += self._nexthop_group(gid, family)
        return nhs

    def route_get(self, dst, src=None, iif=None):
        cmd = f"ip route get {dst}"
        if src:
            cmd += f" from {src}"
        if iif:
            cmd += f" iif {iif}"
        return self.sh(cmd, check=False)

    def chosen_nexthop(self, dst, src, iif):
        toks = self.route_get(dst, src=src, iif=iif).split()
        return toks[toks.index("via") + 1] if "via" in toks else None

    # -- neighbours / links ---------------------------------------------

    def dev_for_nexthop(self, nh):
        code, out = self.exec(f"ip route get {nh}")
        toks = out.split()
        if code == 0 and "dev" in toks:
            return toks[toks.index("dev") + 1]
        return None

    def ensure_neigh(self, nexthops):
        """Resolve the given next-hops with dynamic ARP/ND before a burst so
        the kernel does not drop packets while resolving on demand.  (Permanent
        entries are intentionally avoided - resolution is done only where and
        when a test actually needs it.)"""
        for nh in nexthops:
            v6 = ":" in nh
            ping = "ping6" if v6 else "ping"
            for _ in range(6):
                self.sh(f"{ping} -c1 -W1 {nh}", check=False)
                show = self.sh(f"ip neigh show {nh}", check=False)
                if "lladdr" in show and "FAILED" not in show:
                    break
                time.sleep(0.3)

    def link_down(self, iface):
        self.sh(f"ip link set {iface} down")

    def link_up(self, iface):
        self.sh(f"ip link set {iface} up")

    def set_mtu(self, iface, mtu):
        self.sh(f"ip link set {iface} mtu {mtu}")

    def add_vlan(self, parent, vlan_id):
        iface = f"{parent}.{vlan_id}"
        self.sh(f"ip link add link {parent} name {iface} type vlan id {vlan_id}")
        self.sh(f"ip link set {iface} up")
        return iface

    def del_vlan(self, iface):
        self.sh(f"ip link del {iface} 2>/dev/null", check=False)


class Traffic(Node):
    """A ``traffic_gen`` JSON-RPC endpoint reached over docker exec."""

    URL = "http://127.0.0.1:1234"

    def start_server(self):
        """Start the (scapy-free) capture/generator server in the background."""
        self.env.exec_command(
            self.name,
            ["python3", "/usr/testlibs/traffic_gen.py",
             "--host", "127.0.0.1", "--port", "1234"],
            detach=True)

    def rpc(self, method, params=None):
        cmd = ["python3", "/usr/testlibs/traffic_client.py",
               "--url", self.URL, method, json.dumps(params or {})]
        code, out = self.exec(cmd)
        out = out.strip()
        if code != 0:
            raise RuntimeError(f"[{self.name}] rpc {method} failed: {out}")
        try:
            return json.loads(out)
        except Exception:
            return out

    def wait_ready(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.rpc("ping") == "pong":
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    # -- sender ---------------------------------------------------------

    def send_burst(self, dst, gw_mac, iface, gw_ip=None, gw_ip6=None, **kw):
        params = {"dst": dst, "gw_mac": gw_mac, "iface": iface}
        if gw_ip is not None:
            params["gw_ip"] = gw_ip
        if gw_ip6 is not None:
            params["gw_ip6"] = gw_ip6
        params.update(kw)
        return self.rpc("send_burst", params)

    # -- capturer -------------------------------------------------------

    def start_capture(self, dst=None):
        return self.rpc("start_capture", {"dst": dst} if dst else {})

    def stop_capture(self):
        return self.rpc("stop_capture")

    def clear(self):
        return self.rpc("clear_stats")

    def stats(self):
        return self.rpc("stats")
