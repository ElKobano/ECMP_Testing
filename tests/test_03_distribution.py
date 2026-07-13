"""Category 3/4 - traffic distribution across source IPs (DIST-001 .. DIST-006)."""

import pytest


@pytest.mark.distribution
def test_dist_001_each_source_one_interface(topology, dut):
    """DIST-001: many sources are spread over the paths and every source is
    pinned to exactly one egress interface."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    m = topology.measure(src_base="100.64.0.1", src_count=256, wait=2.0)
    assert m.total == 256, m.counts

    s1, s2 = m.srcs["r1"], m.srcs["r2"]
    assert s1 and s2, m.counts                 # both paths used
    assert s1.isdisjoint(s2)                   # no source on two paths
    assert len(s1 | s2) == 256                 # every source accounted for


@pytest.mark.distribution
def test_dist_002_stable_under_nexthop_reorder(topology, dut):
    """DIST-002: reordering the next-hops keeps the *grouping* of sources
    intact - sources that shared a path still share a path (the path label may
    swap, since Linux selects the next-hop by its position in the group)."""
    dut.add_ecmp_route(topology.dst_net, [topology.nexthops[0], topology.nexthops[1]])
    before = topology.measure(src_base="100.64.0.1", src_count=200, wait=2.0)

    dut.add_ecmp_route(topology.dst_net, [topology.nexthops[1], topology.nexthops[0]])
    after = topology.measure(src_base="100.64.0.1", src_count=200, wait=2.0)

    def partition(m):
        return {frozenset(s) for s in m.srcs.values() if s}

    assert partition(before) == partition(after)


@pytest.mark.distribution
def test_dist_003_four_paths_even(topology, dut):
    """DIST-003: with 4 equal paths and many random sources each path gets
    roughly 25% of the sources."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops)

    m = topology.measure(src_base="100.64.0.1", src_count=1000, wait=3.0)
    assert m.total == 1000, m.counts
    for name in topology.sinks:
        assert 0.15 <= m.share(name) <= 0.35, m.counts


@pytest.mark.distribution
def test_dist_004_two_paths_not_skewed(topology, dut):
    """DIST-004: with 2 paths no single path takes more than 60%."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    m = topology.measure(src_base="100.64.0.1", src_count=1000, wait=3.0)
    assert m.total == 1000, m.counts
    assert max(m.share("r1"), m.share("r2")) <= 0.60, m.counts


@pytest.mark.distribution
def test_dist_005_ipv6_single_source_single_path(topology, dut):
    """DIST-005: IPv6 ECMP - one source always uses one path."""
    dut.add_ecmp_route(topology.dst6_net, topology.nexthops6[:2], family="ipv6")

    m = topology.measure(srcs=["2001:db8:aaaa::5"], per=50, v6=True)
    assert m.paths_used and len(m.paths_used) == 1, m.counts
    assert m.total == 50, m.counts


@pytest.mark.distribution
def test_dist_006_ipv6_four_paths_even(topology, dut):
    """DIST-006: IPv6 ECMP over 4 paths distributes many sources."""
    dut.add_ecmp_route(topology.dst6_net, topology.nexthops6, family="ipv6")

    srcs = [f"2001:db8:aaaa::{i:x}" for i in range(1, 401)]
    m = topology.measure(srcs=srcs, v6=True, wait=3.0)
    assert m.total == 400, m.counts
    assert len(m.paths_used) == 4, m.counts
    for name in topology.sinks:
        assert m.share(name) >= 0.10, m.counts
