"""Category 5 - dynamic add/remove of paths (DYN-001 .. DYN-006)."""

import time

import pytest


def _nexthop_of(topology, sink_name):
    return topology.nexthop_of(sink_name)


def _only_path(m, total):
    used = m.paths_used
    assert used and len(used) == 1 and m.counts[used[0]] == total, m.counts
    return used[0]


@pytest.mark.dynamic
def test_dyn_001_link_down_shifts_traffic(topology, dut):
    """DYN-001: shutting the interface a flow uses moves it to the other path."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    src = ["10.1.0.5"]
    path = _only_path(topology.measure(srcs=src, per=40), 40)

    dev = dut.dev_for_nexthop(_nexthop_of(topology, path))
    dut.link_down(dev)
    time.sleep(1)

    m = topology.measure(srcs=src, per=40)
    new = _only_path(m, 40)
    assert new != path, (path, new, m.counts)


@pytest.mark.dynamic
def test_dyn_002_link_restore_no_duplication(topology, dut):
    """DYN-002: after restoring the interface the flow is delivered on exactly
    one path (no duplication)."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])
    src = ["10.1.0.5"]
    path = _only_path(topology.measure(srcs=src, per=40), 40)
    dev = dut.dev_for_nexthop(_nexthop_of(topology, path))

    dut.link_down(dev)
    time.sleep(1)
    _only_path(topology.measure(srcs=src, per=40), 40)

    dut.link_up(dev)
    time.sleep(1)
    _only_path(topology.measure(srcs=src, per=40), 40)   # still exactly one path


@pytest.mark.dynamic
def test_dyn_003_add_third_path(topology, dut):
    """DYN-003: adding a third equal path makes it available to new sources."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])
    topology.measure(src_base="100.64.0.1", src_count=300, wait=2.0)

    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:3])
    m = topology.measure(src_base="100.64.0.1", src_count=300, wait=2.0)

    assert m.total == 300, m.counts
    assert m.counts["r3"] > 0, m.counts          # new path is used
    assert len(m.paths_used) == 3, m.counts


@pytest.mark.dynamic
def test_dyn_004_remove_all_but_one(topology, dut):
    """DYN-004: reducing the group to a single path sends everything one way,
    with no loss."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops)
    topology.measure(src_base="100.64.0.1", src_count=300, wait=2.0)

    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:1])
    m = topology.measure(src_base="100.64.0.1", src_count=300, wait=2.0)

    assert m.total == 300, m.counts                 # no loss
    assert m.paths_used == ["r1"], m.counts


@pytest.mark.dynamic
def test_dyn_005_raise_metric_removes_path(topology, dut):
    """DYN-005: raising a path's admin distance removes it from the ECMP
    group (only the better path forwards)."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])
    assert len(topology.measure(src_base="100.64.0.1", src_count=200).paths_used) == 2

    # nh2 gets a worse distance -> only nh1 remains active
    dut.del_route(topology.dst_net)
    dut.add_route(topology.dst_net, topology.nexthops[0], distance=10)
    dut.add_route(topology.dst_net, topology.nexthops[1], distance=20)

    m = topology.measure(src_base="100.64.0.1", src_count=200)
    assert m.paths_used == ["r1"], m.counts


@pytest.mark.infra("bgp")
@pytest.mark.dynamic
def test_dyn_006_bgp_neighbor_down(topology, dut):
    """DYN-006: when a BGP neighbour goes away its routes are withdrawn from
    the FIB and traffic moves to the remaining path (infra/bgp)."""
    assert dut.wait_fib(topology.dst_net, 2), dut.show_route(topology.dst_net)

    # simulate the peer dropping
    dut.configure("router bgp 65000", "neighbor 10.0.1.2 shutdown")
    deadline = time.time() + 30
    while time.time() < deadline:
        if set(dut.fib_nexthops(topology.dst_net)) == {"10.0.2.2"}:
            break
        time.sleep(2)
    assert set(dut.fib_nexthops(topology.dst_net)) == {"10.0.2.2"}, \
        dut.show_route(topology.dst_net)

    m = topology.measure(src_base="100.64.0.1", src_count=200)
    assert m.paths_used == ["r2"], m.counts
