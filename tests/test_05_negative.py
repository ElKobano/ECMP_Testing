"""Category 8 - negative scenarios (NEG-001 .. NEG-005)."""

import pytest


@pytest.mark.negative
def test_neg_001_no_route_is_dropped(topology, dut):
    """NEG-001: with no usable next-hop the traffic is dropped."""
    dut.blackhole(topology.dst_net)

    m = topology.measure(src_base="100.64.0.1", src_count=100)
    assert m.total == 0, m.counts


@pytest.mark.negative
def test_neg_002_invalid_source_no_crash(topology, dut):
    """NEG-002: bogus source addresses (multicast/broadcast/0.0.0.0) are
    handled without crashing the router."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    topology.measure(srcs=["224.0.0.5", "255.255.255.255", "0.0.0.0"], per=20)

    # router survived and still forwards legitimate traffic
    assert dut.exec("true")[0] == 0
    good = topology.measure(srcs=["100.64.0.1"], per=20)
    assert good.total == 20, good.counts


@pytest.mark.negative
def test_neg_003_source_equals_destination_no_loop(topology, dut):
    """NEG-003: source == destination is forwarded once, with no loop."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    m = topology.measure(srcs=[topology.dst_ip], per=30)
    assert dut.exec("true")[0] == 0
    # delivered on a single path and not amplified (no loop)
    assert m.total <= 30, m.counts
    assert len(m.paths_used) <= 1, m.counts


@pytest.mark.negative
def test_neg_004_recursive_nexthop(topology, dut):
    """NEG-004: a route whose next-hop is itself reachable only via an ECMP
    group resolves recursively and forwards without looping."""
    # transit prefix reachable via the two real next-hops (ECMP)...
    dut.add_ecmp_route("192.0.2.0/24", topology.nexthops[:2])
    # ...and the destination points at a recursive next-hop inside it.
    dut.add_route(topology.dst_net, "192.0.2.1")

    fib = set(dut.fib_nexthops(topology.dst_net))
    assert fib <= {"10.0.1.2", "10.0.2.2"} and fib, dut.show_route(topology.dst_net)

    m = topology.measure(src_base="100.64.0.1", src_count=100)
    assert dut.exec("true")[0] == 0
    assert m.total == 100, m.counts                 # delivered, no loop/black-hole
    assert set(m.paths_used) <= {"r1", "r2"}, m.counts


@pytest.mark.negative
def test_neg_005_asymmetric_mtu(topology, dut):
    """NEG-005: a packet larger than a path's MTU (DF set) is not forwarded on
    that path (ICMP frag-needed), while normal packets are."""
    dut.add_route(topology.dst_net, topology.nexthops[0])   # single path via r1
    dev = dut.dev_for_nexthop(topology.nexthops[0])
    dut.set_mtu(dev, 1000)                                  # small egress MTU
    try:
        # ~1228-byte IP packet: fits the ingress link but exceeds the 1000-byte
        # egress MTU, so with DF it is dropped (ICMP frag-needed).
        big = topology.measure(srcs=["10.1.0.5"], per=20, proto="udp",
                               size=1200, df=True)
        assert big.total == 0, big.counts

        small = topology.measure(srcs=["10.1.0.5"], per=20, proto="udp", size=100)
        assert small.total == 20, small.counts      # normal traffic forwarded
    finally:
        dut.set_mtu(dev, 1500)
    assert dut.exec("true")[0] == 0
