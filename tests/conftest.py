"""Pytest fixtures for the ECMP source-IP hashing suite.

Infrastructure selection:

    for every test we look for a test-specific infrastructure description in
    ``infra/<test-name>/``; if there is none we fall back to ``infra/basic``.

Scenario directories are fully self-contained — each must contain its own
``infra.yaml``, ``daemons``, and ``configs/`` files.  A scenario is built once
and reused until a test needs a different one.

Router nodes, link names, nexthop IPs, and IPv6 addresses are derived
dynamically from ``infra.yaml`` so that a scenario can declare any number of
routers.
"""

import json
import os
import sys
import time

import pytest
import yaml

CUR_DIR = os.path.dirname(os.path.abspath(__file__))
LIBS_DIR = os.path.abspath(os.path.join(CUR_DIR, "..", "libs"))
INFRA_DIR = os.path.join(CUR_DIR, "infra")
sys.path.insert(0, LIBS_DIR)

from docker_env import DockerEnvironment          # noqa: E402
from ecmp import Dut, FrrNode, Traffic            # noqa: E402

# -- addressing ---------------------------------------------------------

CLIENT_LINK_IP = "203.0.113.1"
CLIENT_IP = "203.0.113.10"
CLIENT_LINK_IP6 = "fd00::1"
CLIENT_IP6 = "fd00::10"
DST_NET, DST_IP = "198.51.100.0/24", "198.51.100.1"
DST6_NET, DST6_IP = "2001:db8::/64", "2001:db8::1"


def _resolve_topo(spec):
    containers = spec.get("containers", {})

    router_names = sorted(
        n for n, p in containers.items() if p and p.get("role") == "router"
    )

    nexthops = []
    nexthops6 = []
    v6_dut = {}
    v6_router = {}
    dut_local = {}

    dut_nets = (containers.get("dut") or {}).get("networks") or {}

    for rname in router_names:
        rnets = (containers.get(rname) or {}).get("networks") or {}
        for link, ip in rnets.items():
            idx = int(link[1:])
            nexthops.append(ip)
            nexthops6.append(f"fd00:{idx}::2")
            v6_dut[link] = f"fd00:{idx}::1/64"
            v6_router[rname] = f"fd00:{idx}::2/64"
            dut_local[rname] = dut_nets.get(link)

    return {
        "router_names": router_names,
        "nexthops": nexthops,
        "nexthops6": nexthops6,
        "v6_dut": v6_dut,
        "v6_router": v6_router,
        "dut_local": dut_local,
    }


class Measurement:
    def __init__(self, sinks):
        self.stats = {name: t.stats() for name, t in sinks.items()}

    @property
    def counts(self):
        return {n: s["total"] for n, s in self.stats.items()}

    @property
    def total(self):
        return sum(self.counts.values())

    @property
    def srcs(self):
        return {n: set(s["src_set"]) for n, s in self.stats.items()}

    @property
    def paths_used(self):
        return [n for n, c in self.counts.items() if c > 0]

    def share(self, name):
        return self.counts[name] / self.total if self.total else 0.0


class Topology:
    def __init__(self, env, infra_name):
        self.env = env
        self.infra_name = infra_name
        self.infra_dir = os.path.join(INFRA_DIR, infra_name)
        with open(os.path.join(self.infra_dir, "infra.yaml")) as f:
            self.spec = yaml.safe_load(f)

        t = _resolve_topo(self.spec)
        self.router_names = t["router_names"]
        self.nexthops = t["nexthops"]
        self.nexthops6 = t["nexthops6"]
        self._v6_dut = t["v6_dut"]
        self._v6_router = t["v6_router"]
        self._dut_local = t["dut_local"]

        self.dut = Dut(env, "dut")
        self.client = Traffic(env, "client")
        self.sinks = {n: Traffic(env, n) for n in self.router_names}
        self.routers = {n: FrrNode(env, n) for n in self.router_names}
        self.dst_net, self.dst_ip = DST_NET, DST_IP
        self.dst6_net, self.dst6_ip = DST6_NET, DST6_IP
        self.dut_mac = self.client_iface = self.dut_ingress_iface = None

    def nexthop_of(self, sink_name):
        return self.nexthops[self.router_names.index(sink_name)]

    # -- lifecycle ------------------------------------------------------

    def _created(self):
        return list(self.spec.get("containers", {})), list(self.spec.get("networks", {}))

    def teardown(self):
        cts, nets = self._created()
        for name in cts:
            try:
                self.env.remove_container(name, force=True)
            except Exception:
                pass
        for name in nets:
            try:
                self.env.remove_network(name)
            except Exception:
                pass

    def build(self):
        self.teardown()
        libs_vol = {LIBS_DIR: {"bind": "/usr/testlibs", "mode": "ro"}}
        daemons = os.path.join(self.infra_dir, "daemons")

        for name, params in self.spec.get("networks", {}).items():
            self.env.create_network(name, **(params or {}))

        for name, params in self.spec.get("containers", {}).items():
            params = dict(params or {})
            role = params.pop("role", None)
            vols = {}
            if role in ("dut", "router"):
                conf = os.path.join(self.infra_dir, "configs", f"{name}.conf")
                vols[daemons] = {"bind": "/cfg/daemons", "mode": "ro"}
                vols[conf] = {"bind": "/cfg/frr.conf", "mode": "ro"}
            if role in ("router", "client"):
                vols.update(libs_vol)
            if vols:
                params["volumes"] = vols
            self.env.create_container(name=name, **params)

        time.sleep(8)  # let systemd finish booting before touching FRR
        self.configure()

    def configure(self):
        frr_nodes = [self.dut] + list(self.routers.values())
        for node in frr_nodes:
            node.install_frr_config()
        for node in frr_nodes:
            node.start_frr()
        if not self.dut.wait_daemons(20):
            raise RuntimeError("DUT FRR daemons did not come up")
        for node in frr_nodes:
            node.leave_mgmt_vrf()

        self.dut.base_setup()
        self.dut.enable_ipv6()
        self.client.enable_ipv6()
        cli6_dev = self.dut.iface_for_ip(CLIENT_LINK_IP)
        if cli6_dev:
            self.dut.add_addr(f"{CLIENT_LINK_IP6}/64", cli6_dev, family="ipv6")
        cli6_dev_c = self.client.iface_for_ip(CLIENT_IP)
        if cli6_dev_c:
            self.client.add_addr(f"{CLIENT_IP6}/64", cli6_dev_c, family="ipv6")
        for link, addr in self._v6_dut.items():
            rname = "r" + link[1]
            if rname in self._dut_local and self._dut_local[rname] is not None:
                dev = self.dut.iface_for_ip(self._dut_local[rname])
                if dev:
                    self.dut.add_addr(addr, dev, family="ipv6")
        for name, node in self.routers.items():
            node.enable_ipv6()
            dev = node.iface_for_ip(self.nexthop_of(name))
            if dev:
                node.add_addr(self._v6_router[name], dev, family="ipv6")

        for t in self.sinks.values():
            t.disable_offloading()
        self.client.disable_offloading()
        self.dut.disable_offloading()

        for t in self.sinks.values():
            t.start_server()
        self.client.wait_ready()
        for t in self.sinks.values():
            t.wait_ready()

        self.dut_mac = self.dut.mac_for_ip(CLIENT_LINK_IP)
        self.client_iface = self.client.iface_for_ip(CLIENT_IP)
        self.dut_ingress_iface = self.dut.iface_for_ip(CLIENT_LINK_IP)

    def reset(self):
        self.dut.clear_static_routes()
        self.dut.set_hash_mode("l3", "ipv4")
        self.dut.set_hash_mode("l3", "ipv6")
        for ip in self._dut_local.values():
            if ip is not None:
                dev = self.dut.iface_for_ip(ip)
                if dev:
                    self.dut.link_up(dev)
        for t in self.sinks.values():
            t.stop_capture()
            t.clear()

    # -- traffic --------------------------------------------------------

    def measure(self, src_base=None, src_count=1, srcs=None, dst=None,
                proto="udp", per=1, v6=False, vary_sport=False,
                size=0, df=False, vlan=None, wait=1.5):
        dst = dst or (self.dst6_ip if v6 else self.dst_ip)
        self.dut.ensure_neigh(self.nexthops6 if v6 else self.nexthops)
        for t in self.sinks.values():
            t.stop_capture()
            t.clear()
            t.start_capture(dst=dst)
        time.sleep(0.3)
        params = {"proto": proto, "count_per_src": per,
                  "vary_sport": vary_sport, "v6": v6, "size": size, "df": df}
        if vlan is not None:
            params["vlan"] = vlan
        if srcs is not None:
            params["srcs"] = srcs
        else:
            params.update(src_base=src_base, src_count=src_count)
        self.client.send_burst(dst, self.dut_mac, self.client_iface,
                               gw_ip=CLIENT_LINK_IP, gw_ip6=CLIENT_LINK_IP6, **params)
        time.sleep(wait)
        for t in self.sinks.values():
            t.stop_capture()
        return Measurement(self.sinks)

    def collect_artifacts(self, test_name, base_dir):
        """Persist FRR configs, route tables, hash state, logs and sink
        statistics for the given *test_name* into *base_dir*."""
        test_dir = os.path.join(base_dir, test_name)
        os.makedirs(test_dir, exist_ok=True)

        _write = lambda name, content: open(
            os.path.join(test_dir, name), "w").write(content)

        try:
            _write("dut_frr.conf",
                   self.dut.sh("vtysh -c 'show running-config'", check=False))
        except Exception:
            pass

        try:
            _write("dut_routes.txt",
                   self.dut.sh("ip route show table all", check=False))
        except Exception:
            pass

        try:
            v4 = self.dut.sh(
                "sysctl net.ipv4.fib_multipath_hash_policy "
                "net.ipv4.fib_multipath_hash_fields", check=False)
            v6 = self.dut.sh(
                "sysctl net.ipv6.fib_multipath_hash_policy "
                "net.ipv6.fib_multipath_hash_fields", check=False)
            _write("dut_hash.txt", f"--- IPv4 ---\n{v4}\n--- IPv6 ---\n{v6}\n")
        except Exception:
            pass

        try:
            _write("dut_frr.log",
                   self.dut.sh("journalctl --no-pager -u frr -n 300 2>/dev/null || true",
                               check=False))
        except Exception:
            pass

        sinks_dir = os.path.join(test_dir, "sinks")
        os.makedirs(sinks_dir, exist_ok=True)
        for name, t in self.sinks.items():
            try:
                st = t.stats()
                with open(os.path.join(sinks_dir, f"{name}.json"), "w") as f:
                    json.dump(st, f, indent=2, ensure_ascii=False)
            except Exception:
                pass


class InfraManager:
    """Builds a scenario topology on demand and reuses it until a different
    scenario is requested."""

    def __init__(self, env):
        self.env = env
        self.current = None
        self.topo = None

    def ensure(self, infra_name):
        if self.current != infra_name:
            if self.topo is not None:
                self.topo.teardown()
            self.topo = Topology(self.env, infra_name)
            self.topo.build()
            self.current = infra_name
        return self.topo

    def teardown(self):
        if self.topo is not None:
            self.topo.teardown()
            self.topo = None
            self.current = None


# --- pytest hooks ---------------------------------------------------------------


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


# --- fixtures -------------------------------------------------------------------


@pytest.fixture(scope="session")
def env():
    return DockerEnvironment()


@pytest.fixture(scope="session")
def _manager(env):
    mgr = InfraManager(env)
    yield mgr
    mgr.teardown()


@pytest.fixture()
def topology(request, _manager):
    marker = request.node.get_closest_marker("infra")
    if marker:
        infra_name = marker.args[0]
    else:
        name = request.node.name
        infra_name = name if os.path.isdir(os.path.join(INFRA_DIR, name)) else "basic"
    topo = _manager.ensure(infra_name)
    topo.reset()
    yield topo

    failed = getattr(request.node, "rep_call", None)
    if failed is not None and failed.failed:
        dest = os.environ.get("ARTIFACTS_DIR",
                              os.path.join(LIBS_DIR, "..", "artifacts", "latest"))
        topo.collect_artifacts(name, dest)

    topo.reset()


@pytest.fixture()
def dut(topology):
    return topology.dut
