"""Category 9 & 6 — configuration and hash-field combination tests
(CFG-001 .. CFG-003, COMBO-001 .. COMBO-002)."""

import time

import pytest


def _nexthops_over_dsts(dut, iif, src, dsts):
    """Which next-hops the DUT selects for one source across several dsts."""
    return {dut.chosen_nexthop(d, src, iif) for d in dsts}


@pytest.mark.config
def test_cfg_001_source_ip_only(topology, dut):
    """CFG-001: 'source-ip only' hashing - path depends solely on the source."""
    dut.set_hash_mode("source-ip")
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    # many sources -> spread across both paths, each pinned to one
    m = topology.measure(src_base="100.64.0.1", src_count=256, wait=2.0)
    assert len(m.paths_used) == 2, m.counts
    assert m.srcs["r1"].isdisjoint(m.srcs["r2"])

    # one source, many destinations -> always the same path (dst ignored)
    dsts = [f"198.51.100.{i}" for i in range(1, 21)]
    chosen = _nexthops_over_dsts(dut, topology.dut_ingress_iface, "10.1.0.5", dsts)
    assert len(chosen) == 1, chosen


@pytest.mark.config
def test_cfg_002_switch_algorithm_live(topology, dut):
    """CFG-002: switching the hash algorithm changes path selection for new
    flows."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])
    dsts = [f"198.51.100.{i}" for i in range(1, 33)]
    iif = topology.dut_ingress_iface

    dut.set_hash_mode("source-ip")
    only_src = _nexthops_over_dsts(dut, iif, "10.1.0.5", dsts)
    assert len(only_src) == 1, only_src           # dst does not matter

    dut.set_hash_mode("src-dst-ip")
    with_dst = _nexthops_over_dsts(dut, iif, "10.1.0.5", dsts)
    assert len(with_dst) > 1, with_dst            # dst now influences the path


@pytest.mark.config
def test_combo_001_source_only_ignores_dst_and_ports(topology, dut):
    """COMBO-001: with source-ip hashing, one source to many dst/ports uses a
    single path."""
    dut.set_hash_mode("source-ip")
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    m = topology.measure(srcs=["10.1.0.5"], per=40, vary_sport=True)
    assert len(m.paths_used) == 1 and m.total == 40, m.counts

    dsts = [f"198.51.100.{i}" for i in range(1, 21)]
    assert len(_nexthops_over_dsts(dut, topology.dut_ingress_iface, "10.1.0.5", dsts)) == 1


@pytest.mark.config
def test_combo_002_source_only_vs_5tuple(topology, dut):
    """COMBO-002: same source, different destinations - one path under
    source-only, several under a wider hash."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops)
    dsts = [f"198.51.100.{i}" for i in range(1, 33)]
    iif = topology.dut_ingress_iface

    dut.set_hash_mode("source-ip")
    assert len(_nexthops_over_dsts(dut, iif, "10.1.0.5", dsts)) == 1

    dut.set_hash_mode("src-dst-ip")
    assert len(_nexthops_over_dsts(dut, iif, "10.1.0.5", dsts)) > 1


@pytest.mark.config
def test_cfg_003_persist_across_reboot(topology, dut):
    """CFG-003: an ECMP configuration saved to FRR (write memory) is restored
    after the routing stack restarts."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])
    dut.vtysh("write memory")

    dut.exec("systemctl restart frr")
    time.sleep(4)
    dut.leave_mgmt_vrf()
    dut.base_setup()

    assert dut.wait_fib(topology.dst_net, 2, timeout=30), dut.show_route(topology.dst_net)
    m = topology.measure(src_base="100.64.0.1", src_count=200)
    assert len(m.paths_used) == 2, m.counts
