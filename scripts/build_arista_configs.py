#!/usr/bin/env python3
"""
Arista vEOS Minimum Startup Config Injector
Directory: /home/dakenrick/Documents/EVE-NG/sdwan-cisco-labs/scripts/build_arista_configs.py

Pushes a Day-0 startup-config to each vEOS node and sets the node to boot from it.
Every API response is checked; lab-lock errors (60061) are retried with backoff,
and an HTML (PHP crash) response stops the run before it can cascade.
"""

import os
import re
import sys
import json
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EVE_IP = os.getenv("EVE_IP", "192.168.1.117")
EVE_USER = os.getenv("EVE_USER", "admin")
EVE_PASS = os.getenv("EVE_PASS", "eve")
LAB_NAME = "SDWAN_AAR_EVPN_Lab.unl"
LAB_PATH = f"/Shared/{LAB_NAME}"

BASE_URL = f"https://{EVE_IP}/api"
SESSION = requests.Session()
SESSION.verify = False
SESSION.headers.update({"Content-Type": "application/json"})

CFSID = os.getenv("EVE_CFSID", "default")   # EVE-NG Pro startup-config set

LOCK_RETRIES = 5       # attempts when EVE returns 60061 (lab lock busy)
LOCK_BACKOFF = 2.0      # seconds, doubled each retry
PACE = 0.5              # pause between API writes so EVE releases the lab lock
CONFIG_SET = os.getenv("EVE_CFSID", "default")   # EVE Pro startup-config set

# Management IP Schema (Subnet: 192.168.100.0/24, Gateway: 192.168.100.1)
ARISTA_NODES = {
    "vEOS-Spine-01": "192.168.100.20",
    "vEOS-Spine-02": "192.168.100.21",
    "vEOS-Leaf-01":  "192.168.100.22",
    "vEOS-Leaf-02":  "192.168.100.23",
    "vEOS-WAN-01":   "192.168.100.24",
    "vEOS-WAN-02":   "192.168.100.25",
}


class EveCrash(Exception):
    """EVE returned an HTML error page instead of JSON (PHP error/500)."""


def short(text, limit=300):
    """Strip HTML tags and collapse whitespace so error pages are readable."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_body(text):
    """Parse EVE's JSON response. EVE Pro 7.x can append/prepend a PHP print_r()
    debug dump ('Array ( ... )') to the JSON, so pull out the first JSON object."""
    text = text or ""
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
            if isinstance(obj, dict) and ("code" in obj or "status" in obj):
                return obj
        except ValueError:
            continue
    raise ValueError("no JSON object in response")


def api(method, path, payload=None, label=""):
    """Call the EVE API, retrying on lab-lock errors. Returns (ok, body_dict)."""
    url = f"{BASE_URL}{path}"
    delay = LOCK_BACKOFF
    for attempt in range(1, LOCK_RETRIES + 1):
        res = SESSION.request(method, url, data=json.dumps(payload) if payload is not None else None)
        try:
            body = parse_body(res.text)
        except ValueError:
            raise EveCrash(f"{label}: HTTP {res.status_code} from {method} {path} -> {short(res.text)}")

        if res.status_code in (200, 201) or body.get("status") == "success":
            time.sleep(PACE)
            return True, body

        if "60061" in str(body.get("message", "")) and attempt < LOCK_RETRIES:
            print(f"    [~] {label}: lab locked, retry {attempt}/{LOCK_RETRIES - 1} in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
            continue

        return False, body
    return False, body


def eve_login():
    res = SESSION.post(f"{BASE_URL}/auth/login",
                       data=json.dumps({"username": EVE_USER, "password": EVE_PASS, "html5": "-1"}))
    if res.status_code != 200 or res.json().get("code") != 200:
        print(f"[-] Login failed: {short(res.text)}")
        sys.exit(1)
    print(f"[+] Authenticated to EVE-NG at {EVE_IP}")


def eve_logout():
    try:
        SESSION.get(f"{BASE_URL}/auth/logout")
    except requests.RequestException:
        pass


def get_nodes():
    """Retrieve all nodes to map names to their current EVE IDs."""
    ok, body = api("GET", f"/labs{LAB_PATH}/nodes", label="list nodes")
    if not ok:
        print(f"[-] Failed to get nodes: {body}")
        sys.exit(1)
    return body.get("data", {}) or {}


def generate_veos_config(hostname, mgmt_ip):
    """Generates the minimum Arista Day-0 configuration."""
    return f"""! device: {hostname}
!
transceiver qsfp default-mode 4x10G
!
hostname {hostname}
!
spanning-tree mode mstp
!
no aaa root
username admin privilege 15 role network-admin secret admin
!
interface Management1
   description OOB_Management
   ip address {mgmt_ip}/24
   no shutdown
!
ip route 0.0.0.0/0 192.168.100.1
!
management api http-commands
   no shutdown
!
end
"""


def inject_config(node_id, node_name, config_text):
    """Push the config payload, then flag the node to boot from it."""
    # EVE Pro 7.x requires the config-set ID (cfsid); the GUI sends "default"
    ok, body = api("PUT", f"/labs{LAB_PATH}/configs/{node_id}",
                   {"id": str(node_id), "data": config_text, "cfsid": CONFIG_SET},
                   label=f"{node_name} config")
    if not ok:
        print(f"[-] Failed to push config for {node_name}: {body.get('message', body)}")
        return False

    ok, body = api("PUT", f"/labs{LAB_PATH}/nodes/{node_id}",
                   {"id": int(node_id), "config": "1"}, label=f"{node_name} startup flag")
    if not ok:
        print(f"[-] Config pushed but startup flag failed for {node_name}: {body.get('message', body)}")
        return False

    print(f"[+] Injected config for {node_name} (Node ID {node_id})")
    return True


def main():
    print("=" * 65)
    print("      ARISTA vEOS DAY-0 CONFIGURATION INJECTOR      ")
    print("=" * 65)

    eve_login()
    try:
        nodes_data = get_nodes()
        name_to_id = {info["name"]: nid for nid, info in nodes_data.items()}

        results = {}
        for hostname, mgmt_ip in ARISTA_NODES.items():
            node_id = name_to_id.get(hostname)
            if not node_id:
                print(f"[!] Node '{hostname}' not found in lab. Skipping.")
                results[hostname] = False
                continue
            results[hostname] = inject_config(node_id, hostname, generate_veos_config(hostname, mgmt_ip))

        done = [h for h, ok in results.items() if ok]
        print(f"\n[*] {len(done)}/{len(ARISTA_NODES)} vEOS nodes configured.")

    except EveCrash as err:
        print(f"\n[-] EVE returned an error page, stopping so it doesn't leave more stale locks:\n    {err}")
        print("    Check the PHP error on the EVE server:  tail -n 50 /var/log/apache2/error.log")
        print("    Then clear any stale lab lock before re-running (see find/rm commands).")
        sys.exit(2)
    finally:
        eve_logout()


if __name__ == "__main__":
    main()