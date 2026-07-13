"""Category 1 - ECMP route installation (test_plan: ECMP-001 .. ECMP-005).

Routes are driven through FRR (staticd for the equal/unequal-cost cases; the
OSPF and BGP scenarios use the ready-made frr.conf files under
infra/ospf/ and infra/bgp/).
"""

import pytest


@pytest.mark.routes
def test_ecmp_001_two_equal_paths(topology, dut):
    """ECMP-001: two equal-cost static paths are both installed in the FIB."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    fib = set(dut.fib_nexthops(topology.dst_net))
    assert fib == set(topology.nexthops[:2]), dut.show_route(topology.dst_net)

    m = topology.measure(src_base="100.64.0.1", src_count=200)
    assert len(m.paths_used) == 2, m.counts


@pytest.mark.routes
def test_ecmp_002_n_equal_paths(topology, dut):
    """ECMP-002: the maximum configured number of paths are all installed."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops)

    fib = set(dut.fib_nexthops(topology.dst_net))
    assert fib == set(topology.nexthops), dut.show_route(topology.dst_net)

    m = topology.measure(src_base="100.64.0.1", src_count=400, wait=2.5)
    assert len(m.paths_used) == 4, m.counts


@pytest.mark.routes
def test_ecmp_003_different_metric_single_fib(topology, dut):
    """ECMP-003: with unequal admin distance only the best path is used for
    forwarding; the worse one stays in the RIB as a backup."""
    dut.add_route(topology.dst_net, topology.nexthops[0], distance=10)
    dut.add_route(topology.dst_net, topology.nexthops[1], distance=20)

    rib = dut.show_route(topology.dst_net)
    assert "10.0.1.2" in rib and "10.0.2.2" in rib, rib      # both in RIB
    assert set(dut.fib_nexthops(topology.dst_net)) == {topology.nexthops[0]}, rib

    m = topology.measure(src_base="100.64.0.1", src_count=200)
    assert m.paths_used == ["r1"], m.counts


@pytest.mark.infra("ospf")
@pytest.mark.routes
def test_ecmp_004_ospf_equal_cost(topology, dut):
    """ECMP-004: two OSPF neighbours advertising the prefix at equal cost are
    installed as ECMP (infra/ospf)."""
    assert dut.wait_fib(topology.dst_net, 2), dut.show_route(topology.dst_net)
    assert set(dut.fib_nexthops(topology.dst_net)) == {"10.0.1.2", "10.0.2.2"}

    m = topology.measure(src_base="100.64.0.1", src_count=200)
    assert set(m.paths_used) == {"r1", "r2"}, m.counts


@pytest.mark.infra("bgp")
@pytest.mark.routes
def test_ecmp_005_bgp_multipath(topology, dut):
    """ECMP-005: two eBGP peers with equal AS-path length install as multipath
    (infra/bgp)."""
    assert dut.wait_fib(topology.dst_net, 2), dut.show_route(topology.dst_net)
    assert set(dut.fib_nexthops(topology.dst_net)) == {"10.0.1.2", "10.0.2.2"}

    m = topology.measure(src_base="100.64.0.1", src_count=200)
    assert set(m.paths_used) == {"r1", "r2"}, m.counts
