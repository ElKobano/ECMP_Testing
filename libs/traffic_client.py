#!/usr/bin/env python3
"""
Minimal JSON-RPC client for traffic_gen.py.

Usage:
  python3 traffic_client.py [--url http://host:port] ping
  python3 traffic_client.py send_burst '{"dst":"198.51.100.1","iface":"eth0","gw_mac":"...","src_base":"100.64.0.1","src_count":256}'
  python3 traffic_client.py start_capture '{"dst":"198.51.100.1"}'
  python3 traffic_client.py stats
  python3 traffic_client.py stop_capture
"""

import sys, json, argparse, urllib.request


def _call(url, method, params=None, rid=1):
    req = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": rid,
    }).encode()

    r = urllib.request.urlopen(
        urllib.request.Request(url, data=req, headers={"Content-Type": "application/json"})
    )
    resp = json.loads(r.read())
    if "error" in resp:
        raise RuntimeError(resp["error"]["message"])
    return resp.get("result")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("method")
    p.add_argument("params", nargs="?", default="{}")
    a = p.parse_args()

    params = json.loads(a.params) if isinstance(a.params, str) else a.params
    result = _call(a.url, a.method, params)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
