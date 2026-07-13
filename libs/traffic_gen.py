#!/usr/bin/env python3
"""
Minimal traffic generator + packet capture with JSON-RPC server.

Capture is scapy-free (raw AF_PACKET + manual header parsing) so the server
can run in stripped-down router images without scapy.  Sending uses scapy for
packet building and is imported lazily, only on the client that needs it.

Usage:  sudo python3 traffic_gen.py [--port 8080] [--host 0.0.0.0]
"""

import json, time, threading, argparse, socket, struct
from http.server import HTTPServer, BaseHTTPRequestHandler


def _ip_add(base, offset):
    packed = socket.inet_aton(base)
    value = struct.unpack("!I", packed)[0] + offset
    return socket.inet_ntoa(struct.pack("!I", value & 0xFFFFFFFF))


def _expand_srcs(p):
    if p.get("srcs"):
        return list(p["srcs"])
    base = p.get("src_base", p.get("src", "10.100.0.1"))
    count = int(p.get("src_count", 1))
    step = int(p.get("src_step", 1))
    return [_ip_add(base, i * step) for i in range(count)]


def _build_packet(proto, src, dst, sport, dport, v6=False, size=0, df=False):
    from scapy.layers.inet import IP, ICMP, TCP, UDP
    from scapy.layers.inet6 import IPv6
    if v6:
        l3 = IPv6(src=src, dst=dst)
    else:
        l3 = IP(src=src, dst=dst, flags="DF" if df else 0)
    if proto == "ICMP":
        if v6:
            from scapy.layers.inet6 import ICMPv6EchoRequest
            pkt = l3 / ICMPv6EchoRequest()
        else:
            pkt = l3 / ICMP()
    elif proto == "TCP":
        pkt = l3 / TCP(sport=sport, dport=dport, flags="S")
    elif proto == "UDP":
        pkt = l3 / UDP(sport=sport, dport=dport)
    else:
        pkt = l3
    if size:
        pkt = pkt / (b"\x00" * int(size))
    return pkt


# -- packet parsing (no scapy) ------------------------------------------

def _parse_frame(data):
    """Parse an Ethernet frame into {src,dst,proto,sport,dport}."""
    if len(data) < 14:
        return None
    eth_type = struct.unpack("!H", data[12:14])[0]
    payload = data[14:]
    if eth_type == 0x8100:                      # 802.1Q VLAN
        if len(payload) < 4:
            return None
        eth_type = struct.unpack("!H", payload[2:4])[0]
        payload = payload[4:]

    if eth_type == 0x0800:                      # IPv4
        if len(payload) < 20:
            return None
        ihl = (payload[0] & 0x0F) * 4
        proto = payload[9]
        src = socket.inet_ntoa(payload[12:16])
        dst = socket.inet_ntoa(payload[16:20])
        l4 = payload[ihl:]
    elif eth_type == 0x86DD:                     # IPv6
        if len(payload) < 40:
            return None
        proto = payload[6]
        src = socket.inet_ntop(socket.AF_INET6, payload[8:24])
        dst = socket.inet_ntop(socket.AF_INET6, payload[24:40])
        l4 = payload[40:]
    else:
        return None

    info = {"ts": time.time(), "src": src, "dst": dst}
    if proto in (1, 58):
        info["proto"] = "ICMP"
    elif proto == 6 and len(l4) >= 4:
        info["proto"] = "TCP"
        info["sport"], info["dport"] = struct.unpack("!HH", l4[:4])
    elif proto == 17 and len(l4) >= 4:
        info["proto"] = "UDP"
        info["sport"], info["dport"] = struct.unpack("!HH", l4[:4])
    else:
        info["proto"] = "OTHER"
    return info


# -- traffic sender -----------------------------------------------------

def send_burst(p):
    proto = p.get("proto", "icmp").upper()
    dst = p["dst"]
    v6 = bool(p.get("v6", ":" in dst))
    per = int(p.get("count_per_src", 1))
    sport = int(p.get("sport", 12345))
    dport = int(p.get("dport", 80))
    size = int(p.get("size", 0))
    df = bool(p.get("df", False))
    vlan = p.get("vlan")
    srcs = _expand_srcs(p)

    pkts = []
    for s in srcs:
        for i in range(per):
            sp = sport + i if p.get("vary_sport") else sport
            pkts.append(_build_packet(proto, s, dst, sp, dport,
                                      v6=v6, size=size, df=df))

    iface = p["iface"]

    if vlan is not None:
        _send_l2(pkts, iface, p["gw_mac"], vlan, v6)
    else:
        _send_l3(pkts, iface, p.get("gw_ip"), p.get("gw_ip6"), dst, v6)

    return {"sent": len(pkts), "srcs": len(srcs)}


def _send_l2(pkts, iface, gw_mac, vlan, v6):
    from scapy.layers.l2 import Ether, Dot1Q
    src_mac = _iface_mac(iface)
    ethertype = 0x86DD if v6 else 0x0800
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    s.bind((iface, 0))
    try:
        for pkt in pkts:
            frame = Ether(src=src_mac, dst=gw_mac)
            frame /= Dot1Q(vlan=int(vlan), type=ethertype)
            frame /= pkt
            s.send(bytes(frame))
    finally:
        s.close()


def _send_l3(pkts, iface, gw_ip, gw_ip6, dst, v6):
    from scapy.sendrecv import send as scapy_send
    import subprocess

    if gw_ip and not v6:
        subprocess.run(
            ["ip", "route", "replace", dst, "via", gw_ip, "dev", iface],
            capture_output=True)
    elif gw_ip6 and v6:
        subprocess.run(
            ["ip", "-6", "route", "replace", dst, "via", gw_ip6, "dev", iface],
            capture_output=True)
    try:
        for pkt in pkts:
            scapy_send(pkt, iface=iface, verbose=False)
    finally:
        if gw_ip and not v6:
            subprocess.run(
                ["ip", "route", "del", dst, "via", gw_ip, "dev", iface],
                capture_output=True)
        elif gw_ip6 and v6:
            subprocess.run(
                ["ip", "-6", "route", "del", dst, "via", gw_ip6, "dev", iface],
                capture_output=True)


def _iface_mac(iface):
    with open("/sys/class/net/%s/address" % iface) as f:
        return f.read().strip()


# -- packet capturer ----------------------------------------------------

class Capturer:
    def __init__(self):
        self._stop = False
        self._thread = None
        self._pkts = []
        self._lock = threading.Lock()

    def start(self, cfg=None):
        if self._thread and self._thread.is_alive():
            return "capture already running"
        self._stop = False
        self._thread = threading.Thread(target=self._sniff, args=(cfg or {},), daemon=True)
        self._thread.start()
        return "capture started"

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=3)
        return "capture stopped"

    def stats(self):
        with self._lock:
            pkts = list(self._pkts)
        proto_counts = {}
        by_src = {}
        for p in pkts:
            proto = p.get("proto", "OTHER")
            proto_counts[proto] = proto_counts.get(proto, 0) + 1
            src = p.get("src")
            by_src[src] = by_src.get(src, 0) + 1
        return {
            "capturing": bool(self._thread and self._thread.is_alive()),
            "total": len(pkts),
            "by_proto": proto_counts,
            "by_src": by_src,
            "src_set": sorted(by_src.keys()),
            "unique_srcs": len(by_src),
            "packets": pkts[-50:],  # last 50 packets
        }

    def clear(self):
        with self._lock:
            self._pkts.clear()
        return "stats cleared"

    def _sniff(self, cfg):
        # Raw AF_PACKET capture with manual header parsing (no scapy), so the
        # server runs in stripped-down router images too.
        iface = cfg.get("iface") or None
        dst_filter = cfg.get("dst") or None
        proto_filter = (cfg.get("proto") or "").upper() or None

        ETH_P_ALL = 0x0003
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        if iface:
            s.bind((iface, 0))
        s.settimeout(0.5)

        try:
            while not self._stop:
                try:
                    data = s.recv(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                info = _parse_frame(data)
                if not info:
                    continue
                if dst_filter and info.get("dst") != dst_filter:
                    continue
                if proto_filter and info.get("proto") != proto_filter:
                    continue
                with self._lock:
                    self._pkts.append(info)
        finally:
            s.close()


# -- JSON-RPC -----------------------------------------------------------

_capturer = Capturer()

_RPC_MAP = {
    "ping":           lambda _p: "pong",
    "send_burst":     send_burst,
    "start_capture":  lambda p: _capturer.start(p),
    "stop_capture":   lambda _p: _capturer.stop(),
    "stats":          lambda _p: _capturer.stats(),
    "clear_stats":    lambda _p: _capturer.clear(),
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except Exception:
            self._reply(400, _err(None, -32700, "Parse error"))
            return

        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        fn = _RPC_MAP.get(method)
        if fn is None:
            self._reply(200, _err(rid, -32601, f"Method not found: {method}"))
            return

        try:
            result = fn(params)
            self._reply(200, {"jsonrpc": "2.0", "result": result, "id": rid})
        except Exception as e:
            self._reply(200, _err(rid, -32603, str(e)))

    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _err(rid, code, msg):
    return {"jsonrpc": "2.0", "error": {"code": code, "message": msg}, "id": rid}


# -- main ---------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()

    srv = HTTPServer((a.host, a.port), Handler)
    print(f"JSON-RPC on http://{a.host}:{a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _capturer.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
