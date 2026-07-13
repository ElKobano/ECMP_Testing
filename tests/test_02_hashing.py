"""Category 2 - deterministic hashing by source IP (HASH-001 .. HASH-003)."""

import pytest


def _single_path(m, expect_total):
    """Assert all traffic landed on exactly one path; return its name."""
    used = m.paths_used
    assert used and len(used) == 1, m.counts
    assert m.counts[used[0]] == expect_total, m.counts
    return used[0]


@pytest.mark.hashing
def test_hash_001_same_source_same_path(topology, dut):
    """HASH-001: 100 packets from one source, repeated 3x, all take one path."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    paths = []
    for _ in range(3):
        m = topology.measure(srcs=["10.1.0.5"], per=100)
        paths.append(_single_path(m, 100))
    assert len(set(paths)) == 1, f"path changed between runs: {paths}"


@pytest.mark.hashing
def test_hash_002_stable_after_route_clear(topology, dut):
    """HASH-002: after flushing and re-installing the route the same source
    still hashes to the same next-hop."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])
    before = _single_path(topology.measure(srcs=["10.1.0.5"], per=50), 50)

    dut.del_route(topology.dst_net)                       # "clear ip route *"
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    after = _single_path(topology.measure(srcs=["10.1.0.5"], per=50), 50)
    assert before == after, (before, after)


@pytest.mark.hashing
def test_hash_003_source_equals_router_interface(topology, dut):
    """HASH-003: traffic whose source equals one of the DUT interfaces is
    hashed to a single path and does not loop / crash the router."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    m = topology.measure(srcs=["10.0.1.1"], per=50)  # DUT's own p1 address
    _single_path(m, 50)

    # router is still healthy
    assert topology.dut.exec("true")[0] == 0
    nh = dut.chosen_nexthop(topology.dst_ip, "10.0.1.1", dut.iface_for_ip("203.0.113.1"))
    assert nh is not None, f"no next-hop resolved for src=10.0.1.1"
