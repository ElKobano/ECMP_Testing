"""Category 10 - end-to-end scenarios (E2E-001 .. E2E-003)."""

import pytest


@pytest.mark.e2e
def test_e2e_001_tcp_session_single_path(topology, dut):
    """E2E-001: every packet of one TCP flow traverses the same path."""
    dut.set_hash_mode("5-tuple")
    dut.add_ecmp_route(topology.dst_net, topology.nexthops)

    # a single 5-tuple (fixed src/dst/sport/dport)
    m = topology.measure(srcs=["10.1.0.5"], per=60, proto="tcp")
    assert len(m.paths_used) == 1 and m.total == 60, m.counts


@pytest.mark.e2e
def test_e2e_002_nat_single_public_source(topology, dut):
    """E2E-002: clients behind NAT (one public source IP) all take one path."""
    dut.set_hash_mode("source-ip")
    dut.add_ecmp_route(topology.dst_net, topology.nexthops)

    m = topology.measure(srcs=["203.0.113.200"], per=80, vary_sport=True)
    assert len(m.paths_used) == 1 and m.total == 80, m.counts


@pytest.mark.e2e
def test_e2e_003_vlan_does_not_affect_hash(topology, dut):
    """E2E-003: 802.1Q VLAN tags are L2 and do not influence the ECMP hash -
    the same source IP lands on the same path regardless of VLAN tag."""
    dut.add_ecmp_route(topology.dst_net, topology.nexthops[:2])

    vlan_if = dut.add_vlan(topology.dut_ingress_iface, 100)
    try:
        untagged = topology.measure(srcs=["10.1.0.5"], per=40)
        used = untagged.paths_used
        assert used and len(used) == 1 and untagged.total == 40, untagged.counts

        tagged = topology.measure(srcs=["10.1.0.5"], per=40, vlan=100)
        assert tagged.paths_used == used, tagged.counts
        assert tagged.total == 40, tagged.counts
    finally:
        dut.del_vlan(vlan_if)
