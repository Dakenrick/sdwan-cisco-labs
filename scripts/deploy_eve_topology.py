#!/usr/bin/env python3
"""
EVE-NG Topology Provisioner (Full Fabric Wiring + Dual Cloud1/pnet1 OOB Management)
Directory: /home/dakenrick/Documents/EVE-NG/sdwan-cisco-labs/scripts/deploy_eve_topology.py

Builds the full Cisco SD-WAN 20.18.5 + Arista EVPN lab and attaches one
management interface on every node to one of two Cloud1 (pnet1) networks,
splitting paired devices across the two clouds (e.g. WAN-01 -> A, WAN-02 -> B).
"""

import os
import sys
import json
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EVE_IP = os.getenv("EVE_IP", "192.168.1.117")
EVE_USER = os.getenv("EVE_USER", "admin")
EVE_PASS = os.getenv("EVE_PASS", "eve")
LAB_NAME = "SDWAN_AAR_EVPN_Lab.unl"
LAB_PATH = f"/Shared/{LAB_NAME}"

# Two out-of-band management clouds, both EVE "Cloud1" (network type pnet1)
MGMT_CLOUD_TYPE = "pnet1"
MGMT_CLOUDS = {
    "A": {"name": "Cloud1_MGMT_A", "x": 900,  "y": 250},
    "B": {"name": "Cloud1_MGMT_B", "x": 900,  "y": 650},
}

BASE_URL = f"https://{EVE_IP}/api"
SESSION = requests.Session()
SESSION.verify = False

# Tracks (node_id, interface_idx) -> link label so an interface is never
# silently re-wired by a second PUT.
USED_INTERFACES = {}


def eve_login():
    login_url = f"{BASE_URL}/auth/login"
    payload = {"username": EVE_USER, "password": EVE_PASS, "html5": "-1"}
    headers = {"Content-Type": "application/json"}

    res = SESSION.post(login_url, data=json.dumps(payload), headers=headers)
    if res.status_code == 200 and res.json().get("code") == 200:
        print(f"[+] Authenticated to EVE-NG at {EVE_IP}")
        return True
    print(f"[-] Login failed: {res.text}")
    sys.exit(1)


def create_lab():
    url = f"{BASE_URL}/labs"
    payload = {
        "path": "/Shared",
        "name": LAB_NAME.replace(".unl", ""),
        "version": "1",
        "description": "Full Cisco SD-WAN 20.18.5 + Arista EVPN Fabric Topology w/ Dual Cloud1 OOB Mgmt",
    }
    res = SESSION.post(url, data=json.dumps(payload))
    if res.status_code in [200, 201]:
        print(f"[+] Created Lab under /Shared: {LAB_NAME}")
    else:
        print(f"[*] Lab note: {res.json().get('message', res.text)}")


TEMPLATE_ICON_CACHE = {}


def get_template_icon(template):
    """Return the template's default icon (what the EVE GUI would use).

    The API defaults every node to Router.png unless 'icon' is sent, so we
    look up the template's own default via /api/list/templates/<template>.
    """
    if template in TEMPLATE_ICON_CACHE:
        return TEMPLATE_ICON_CACHE[template]

    icon = None
    try:
        res = SESSION.get(f"{BASE_URL}/list/templates/{template}")
        if res.status_code == 200:
            icon = res.json()["data"]["options"]["icon"]["value"]
    except (ValueError, KeyError, TypeError, requests.RequestException):
        pass

    if icon:
        print(f"[*] Template '{template}' default icon: {icon}")
    else:
        print(f"[!] Could not read default icon for template '{template}' -- EVE will use Router.png")
    TEMPLATE_ICON_CACHE[template] = icon
    return icon


def add_node(name, node_type, template, image, cpu, ram, ethernet, x, y, icon=None):
    """Create a node. 'icon' overrides the template default (e.g. icon='Firewall.png')."""
    url = f"{BASE_URL}/labs{LAB_PATH}/nodes"
    icon = icon or get_template_icon(template)
    payload = {
        "type": node_type,
        "template": template,
        "image": image,
        "name": name,
        "cpu": cpu,
        "ram": ram,
        "ethernet": ethernet,
        "left": x,
        "top": y,
        "console": "telnet",
    }
    if icon:
        payload["icon"] = icon
    res = SESSION.post(url, data=json.dumps(payload))
    if res.status_code in [200, 201]:
        node_id = str(res.json()["data"]["id"])
        print(f"[+] Added Node [{node_id}]: {name} ({image}) icon={icon or 'default'} at ({x}, {y})")
        return node_id
    print(f"[-] Failed to add node {name}: {res.text}")
    return None


def add_network(name, net_type, x, y):
    url = f"{BASE_URL}/labs{LAB_PATH}/networks"
    payload = {"type": net_type, "name": name, "left": x, "top": y}
    res = SESSION.post(url, data=json.dumps(payload))
    if res.status_code in [200, 201]:
        return str(res.json()["data"]["id"])
    print(f"[-] Failed to create network {name}: {res.text}")
    return None


def attach(node_id, intf_idx, net_id, label):
    """Attach one node interface to an EVE network (bridge or cloud)."""
    if not node_id or not net_id:
        print(f"    └─ [-] Skipped {label}: missing node or network ID")
        return False

    key = (node_id, intf_idx)
    if key in USED_INTERFACES:
        print(f"    └─ [!] Node {node_id} index {intf_idx} already used by "
              f"'{USED_INTERFACES[key]}' -- refusing to re-wire for '{label}'")
        return False

    url = f"{BASE_URL}/labs{LAB_PATH}/nodes/{node_id}/interfaces"
    res = SESSION.put(url, data=json.dumps({str(intf_idx): str(net_id)}))
    try:
        body = res.json()
    except ValueError:
        body = {}
    # EVE answers interface PUTs with 200 or 201 ("Lab has been saved") depending on version
    if res.status_code in (200, 201) or body.get("status") == "success":
        USED_INTERFACES[key] = label
        return True
    print(f"    └─ [-] Interface mapping failed for {label}: {res.text}")
    return False


def connect_p2p(node1_id, intf1_idx, node2_id, intf2_idx, link_name):
    net_id = add_network(link_name, "bridge", 0, 0)
    if not net_id:
        return
    ok1 = attach(node1_id, intf1_idx, net_id, link_name)
    ok2 = attach(node2_id, intf2_idx, net_id, link_name)
    if ok1 and ok2:
        print(f"  └─ Connected: Node {node1_id} (idx {intf1_idx}) <---> "
              f"Node {node2_id} (idx {intf2_idx}) [{link_name}]")


def connect_to_cloud(node_id, intf_idx, cloud_id, cloud_name, intf_label=None):
    if attach(node_id, intf_idx, cloud_id, cloud_name):
        shown = intf_label or f"idx {intf_idx}"
        print(f"  └─ Connected: Node {node_id} ({shown}) ---> [{cloud_name}]")
        return True
    return False


def main():
    print("=" * 70)
    print("   EVE-NG SD-WAN + EVPN FABRIC PROVISIONER (w/ Dual Cloud1 OOB MGMT)   ")
    print("=" * 70)

    eve_login()
    create_lab()

    nodes = {}

    # 1. SD-WAN CONTROLLERS & SERVICES
    #    vManage: 3 NICs -> eth0 = Leaf-01, eth1 = reserved (future DR), eth2 = MGMT
    #    vSmart : eth0 = Leaf-01, eth1 = MGMT
    #    vBond  : index 0 = eth0 (MGMT), index 1 = ge0/0 (VPN 0 underlay -> Leaf-02)
    print("\n--- Deploying SD-WAN Controllers & Compute Services ---")
    nodes["vManage"]    = add_node("vManage-01", "qemu", "vtmgmt",  "vtmgmt-20.18.5",  4, 16384, 3, 100, 50)
    nodes["vSmart"]     = add_node("vSmart-01",  "qemu", "vtsmart", "vtsmart-20.18.5", 2, 4096,  2, 300, 50)
    nodes["vBond"]      = add_node("vBond-01",   "qemu", "vtbond",  "vtbond-20.18.5",  2, 4096,  2, 500, 50)
    nodes["TestServer"] = add_node("Host-TestServer", "qemu", "linux", "ubuntu-22.04", 1, 2048, 2, 700, 50)

    # 2. ARISTA EVPN / VXLAN FABRIC
    print("\n--- Deploying Arista EVPN/VXLAN Fabric Nodes ---")
    nodes["Spine1"] = add_node("vEOS-Spine-01", "qemu", "veos", "veos-4.36.3.M", 2, 4096, 12, 300, 180)
    nodes["Spine2"] = add_node("vEOS-Spine-02", "qemu", "veos", "veos-4.36.3.M", 2, 4096, 12, 500, 180)
    nodes["Leaf1"]  = add_node("vEOS-Leaf-01",  "qemu", "veos", "veos-4.36.3.M", 2, 4096, 12, 200, 280)
    nodes["Leaf2"]  = add_node("vEOS-Leaf-02",  "qemu", "veos", "veos-4.36.3.M", 2, 4096, 12, 600, 280)

    # 3. WAN CORE TRANSPORTS, MGMT CLOUDS & ARISTA WAN ROUTERS
    print("\n--- Deploying WAN Core Infrastructure & Management Clouds ---")
    inet_cloud = add_network("COMMERCIAL_INTERNET_WAN", "bridge", 100, 680)
    mpls_cloud = add_network("PROVIDER_MPLS_PE",        "bridge", 500, 680)

    mgmt_ids = {}
    for side, cfg in MGMT_CLOUDS.items():
        mgmt_ids[side] = add_network(cfg["name"], MGMT_CLOUD_TYPE, cfg["x"], cfg["y"])
        if mgmt_ids[side]:
            print(f"[+] Created Management Cloud net{mgmt_ids[side]}: {cfg['name']} ({MGMT_CLOUD_TYPE})")

    nodes["vEOS-WAN1"] = add_node("vEOS-WAN-01", "qemu", "veos", "veos-4.36.3.M", 2, 4096, 8, 350, 420)
    nodes["vEOS-WAN2"] = add_node("vEOS-WAN-02", "qemu", "veos", "veos-4.36.3.M", 2, 4096, 8, 550, 420)

    # 4. HUB SITES & FIREWALL
    print("\n--- Deploying Hub WAN Edges & DC1_FW1 ---")
    nodes["Hub1"]    = add_node("c8000v-Hub-01", "qemu", "c8000v",   "c8000v-17.15.06",      2, 8192, 8, 300, 520)
    nodes["Hub2"]    = add_node("c8000v-Hub-02", "qemu", "c8000v",   "c8000v-17.15.06",      2, 8192, 8, 500, 520)
    nodes["DC1_FW1"] = add_node("DC1_FW1",       "qemu", "paloalto", "paloalto-10.2.10-h12", 2, 8192, 6, 150, 520)

    # 5. REMOTE BRANCH SITES & END HOSTS
    print("\n--- Deploying Remote Branch Sites & End Hosts ---")
    nodes["Branch1"]    = add_node("c8000v-Branch-01", "qemu", "c8000v",   "c8000v-17.15.06",      2, 4096, 6, 100, 880)
    nodes["Branch1_FW"] = add_node("FW-Branch-01",     "qemu", "paloalto", "paloalto-10.2.10-h12", 2, 8192, 4, 100, 780)
    nodes["Host_B1"]    = add_node("Host-Branch-01",   "qemu", "linux",    "ubuntu-22.04",         1, 2048, 2, 100, 980)

    nodes["Branch2"]    = add_node("c8000v-Branch-02", "qemu", "c8000v",   "c8000v-17.15.06",      2, 4096, 6, 300, 880)
    nodes["Branch2_FW"] = add_node("FW-Branch-02",     "qemu", "paloalto", "paloalto-10.2.10-h12", 2, 8192, 4, 300, 780)
    nodes["Host_B2"]    = add_node("Host-Branch-02",   "qemu", "linux",    "ubuntu-22.04",         1, 2048, 2, 300, 980)

    nodes["Branch3"]    = add_node("c8000v-Branch-03", "qemu", "c8000v",   "c8000v-17.15.06",      2, 4096, 6, 500, 880)
    nodes["Host_B3"]    = add_node("Host-Branch-03",   "qemu", "linux",    "ubuntu-22.04",         1, 2048, 2, 500, 980)

    # 6. PHYSICAL INTERCONNECT WIRING
    print("\n--- Wiring Physical Infrastructure Interconnects ---")

    # A. Arista MLAG Peer Links (LAST TWO PORTS: eth10 & eth11)
    connect_p2p(nodes["Spine1"], 10, nodes["Spine2"], 10, "link_spine_mlag_eth10")
    connect_p2p(nodes["Spine1"], 11, nodes["Spine2"], 11, "link_spine_mlag_eth11")
    connect_p2p(nodes["Leaf1"],  10, nodes["Leaf2"],  10, "link_leaf_mlag_eth10")
    connect_p2p(nodes["Leaf1"],  11, nodes["Leaf2"],  11, "link_leaf_mlag_eth11")

    # Arista WAN Inter-Router Peer Link (eth6)
    connect_p2p(nodes["vEOS-WAN1"], 6, nodes["vEOS-WAN2"], 6, "link_wan_peer_eth6")

    # B. Controllers -> Leaf Switches
    connect_p2p(nodes["vManage"],    0, nodes["Leaf1"], 1, "link_vmanage_eth0_leaf1")
    connect_p2p(nodes["vSmart"],     0, nodes["Leaf1"], 2, "link_vsmart_eth0_leaf1")
    # vBond index 1 = ge0/0 (VPN 0 underlay); index 0 = eth0 is reserved for MGMT
    connect_p2p(nodes["vBond"],      1, nodes["Leaf2"], 1, "link_vbond_ge00_leaf2")
    connect_p2p(nodes["TestServer"], 0, nodes["Leaf2"], 2, "link_testserver_leaf2")

    # C. EVPN Spine-Leaf Fabric Mesh
    connect_p2p(nodes["Leaf1"], 3, nodes["Spine1"], 1, "link_leaf1_spine1")
    connect_p2p(nodes["Leaf1"], 4, nodes["Spine2"], 1, "link_leaf1_spine2")
    connect_p2p(nodes["Leaf2"], 3, nodes["Spine1"], 2, "link_leaf2_spine1")
    connect_p2p(nodes["Leaf2"], 4, nodes["Spine2"], 2, "link_leaf2_spine2")

    # D. Border Leaf -> Arista WAN Routers & DC1_FW1 (ethernet1/1)
    connect_p2p(nodes["Leaf1"], 5, nodes["vEOS-WAN1"], 1, "link_leaf1_wan1")
    connect_p2p(nodes["Leaf2"], 5, nodes["vEOS-WAN2"], 1, "link_leaf2_wan2")
    connect_p2p(nodes["Leaf1"], 6, nodes["DC1_FW1"],   1, "link_leaf1_fw1")

    # E. Hub c8000v Service-Side LACP Port-Channels (eth2 & eth3 -> Border Leafs)
    connect_p2p(nodes["Hub1"], 2, nodes["Leaf1"], 7, "link_hub1_leaf1_po1")
    connect_p2p(nodes["Hub1"], 3, nodes["Leaf2"], 7, "link_hub1_leaf2_po1")
    connect_p2p(nodes["Hub2"], 2, nodes["Leaf1"], 8, "link_hub2_leaf1_po1")
    connect_p2p(nodes["Hub2"], 3, nodes["Leaf2"], 8, "link_hub2_leaf2_po1")

    # F. Hub c8000v VPN 0 Interfaces (eth0 -> WAN Router, eth1 -> DC1_FW1)
    connect_p2p(nodes["Hub1"], 0, nodes["vEOS-WAN1"], 2, "link_hub1_eth0_wan1_vpn0")
    connect_p2p(nodes["Hub2"], 0, nodes["vEOS-WAN2"], 2, "link_hub2_eth0_wan2_vpn0")
    connect_p2p(nodes["Hub1"], 1, nodes["DC1_FW1"],   2, "link_hub1_eth1_fw1_vpn0")
    connect_p2p(nodes["Hub2"], 1, nodes["DC1_FW1"],   3, "link_hub2_eth1_fw1_vpn0")

    # G. WAN Infrastructure to Transport Clouds
    connect_to_cloud(nodes["vEOS-WAN1"], 3, mpls_cloud, "PROVIDER_MPLS_PE")
    connect_to_cloud(nodes["vEOS-WAN2"], 3, mpls_cloud, "PROVIDER_MPLS_PE")
    # DC1_FW1 untrust on ethernet1/4 (ethernet1/1 is the Leaf1 link)
    connect_to_cloud(nodes["DC1_FW1"],   4, inet_cloud, "COMMERCIAL_INTERNET_WAN")

    # H. Remote Branch Site Connectivity & Local End Hosts
    connect_to_cloud(nodes["Branch1"], 0, mpls_cloud, "PROVIDER_MPLS_PE")
    connect_to_cloud(nodes["Branch2"], 0, mpls_cloud, "PROVIDER_MPLS_PE")
    connect_to_cloud(nodes["Branch3"], 0, mpls_cloud, "PROVIDER_MPLS_PE")

    connect_to_cloud(nodes["Branch1_FW"], 1, inet_cloud, "COMMERCIAL_INTERNET_WAN")
    connect_to_cloud(nodes["Branch2_FW"], 1, inet_cloud, "COMMERCIAL_INTERNET_WAN")

    connect_p2p(nodes["Branch1"], 1, nodes["Branch1_FW"], 2, "link_branch1_eth1_fw_vpn0")
    connect_p2p(nodes["Branch2"], 1, nodes["Branch2_FW"], 2, "link_branch2_eth1_fw_vpn0")

    connect_p2p(nodes["Branch1"], 2, nodes["Host_B1"], 0, "link_branch1_eth2_host")
    connect_p2p(nodes["Branch2"], 2, nodes["Host_B2"], 0, "link_branch2_eth2_host")
    connect_p2p(nodes["Branch3"], 2, nodes["Host_B3"], 0, "link_branch3_eth2_host")

    # 7. OUT-OF-BAND MANAGEMENT -> Dual Cloud1 (pnet1)
    #    node key: (EVE interface index, interface name, cloud side)
    #    Paired devices are split across A/B; 11 nodes on A, 10 on B.
    print(f"\n--- Wiring Out-of-Band Management to "
          f"{MGMT_CLOUDS['A']['name']} / {MGMT_CLOUDS['B']['name']} ({MGMT_CLOUD_TYPE}) ---")
    MGMT_PORTS = {
        # SD-WAN controllers & test server
        "vManage":    (2, "eth2",      "A"),
        "vSmart":     (1, "eth1",      "B"),
        "vBond":      (0, "eth0",      "A"),
        "TestServer": (1, "e1",        "B"),
        # Arista (dedicated Management1 = index 0)
        "Spine1":     (0, "Mgmt1",     "A"),
        "Spine2":     (0, "Mgmt1",     "B"),
        "Leaf1":      (0, "Mgmt1",     "A"),
        "Leaf2":      (0, "Mgmt1",     "B"),
        "vEOS-WAN1":  (0, "Mgmt1",     "A"),
        "vEOS-WAN2":  (0, "Mgmt1",     "B"),
        # c8000v (no dedicated mgmt port) -> standard Gi6 (index 5) on every c8000v, VPN 512
        "Hub1":       (5, "Gi6",       "A"),
        "Hub2":       (5, "Gi6",       "B"),
        "Branch1":    (5, "Gi6",       "A"),
        "Branch2":    (5, "Gi6",       "B"),
        "Branch3":    (5, "Gi6",       "A"),
        # Palo Alto (dedicated mgmt = index 0)
        "DC1_FW1":    (0, "mgmt",      "B"),
        "Branch1_FW": (0, "mgmt",      "A"),
        "Branch2_FW": (0, "mgmt",      "B"),
        # Linux hosts
        "Host_B1":    (1, "e1",        "A"),
        "Host_B2":    (1, "e1",        "B"),
        "Host_B3":    (1, "e1",        "A"),
    }

    missing = [k for k in nodes if k not in MGMT_PORTS]
    if missing:
        print(f"[!] Nodes with no management mapping: {', '.join(missing)}")

    tally = {"A": [], "B": []}
    for key, (idx, ifname, side) in MGMT_PORTS.items():
        cloud = MGMT_CLOUDS[side]["name"]
        if connect_to_cloud(nodes.get(key), idx, mgmt_ids.get(side), cloud, ifname):
            tally[side].append(key)

    print("\n--- Management Distribution Summary ---")
    for side in ("A", "B"):
        print(f"  net{mgmt_ids.get(side)} {MGMT_CLOUDS[side]['name']}: "
              f"{len(tally[side])} nodes -> {', '.join(tally[side])}")

    print("\n[+] EVE-NG Physical Topology + Dual Cloud1 Management Provisioned Successfully!")


if __name__ == "__main__":
    main()