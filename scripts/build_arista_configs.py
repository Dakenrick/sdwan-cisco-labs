#!/usr/bin/env python3
"""
Arista vEOS EVPN/VXLAN Fabric + MPLS WAN Startup-Config Builder & Injector (TSK-05)
Directory: /home/dakenrick/Documents/EVE-NG/sdwan-cisco-labs/scripts/build_arista_configs.py

Renders full startup configs for the hub fabric and pushes them to EVE-NG Pro:
  - vEOS-Spine-01/02 : eBGP underlay + EVPN route servers           (AS 65100)
  - vEOS-Leaf-01/02  : MLAG pair, shared VTEP, VRFs WAN + SERVICE    (AS 65101)
                       (combined compute/service/border leaf pair)
  - vEOS-WAN-01/02   : simulated MPLS provider PE pair               (AS 65000)

Usage:
  python3 build_arista_configs.py            # render to ./configs/arista/ and inject into EVE
  python3 build_arista_configs.py --dry-run  # render files only, no EVE API calls

Startup configs only apply on a node's first boot: Stop + Wipe the vEOS nodes,
then start them after injecting.

======================================================================
 ADDRESSING PLAN
----------------------------------------------------------------------
 OOB mgmt (VRF MGMT, Management1)  192.168.100.0/24  gw .1   Arista .20-.25
 Loopback0 (router-id / EVPN)      10.0.250.0/24     Spines .1-.2, Leafs .11-.12
 Loopback1 (shared MLAG VTEP)      10.0.255.11/32    Leaf-01 + Leaf-02
 Spine<->Leaf P2Ps                 10.0.1.0/24 (/31) spine = even, leaf = odd
 Leaf<->WAN router P2Ps (VRF WAN)  10.0.2.0/24 (/31)
 Leaf-01<->DC1_FW1 eth1/1 (VRF WAN)10.0.3.0/30       leaf .1, FW .2 (default route)
 MLAG peer / iBGP SVIs             10.255.251-254.0/31 (VLAN 4093/4094/4091/4092)

 VRF WAN     (L3VNI 50010) - SD-WAN controller VPN 0 underlay
   VLAN 10   CONTROLLERS   10.10.10.0/24  VNI 10010  gw .1 (VARP)
             vManage .10, vSmart .20, vBond .30
 VRF SERVICE (L3VNI 50020) - SD-WAN service VPN 10 at the hub
   VLAN 100  HUB_SVC_TRANSIT 10.100.100.0/24 VNI 10100  gw .1
             Hub-01 Po1 .11, Hub-02 Po1 .12 (eBGP AS 65200)
   VLAN 200  HUB_SERVERS   10.200.1.0/24  VNI 10200  gw .1  TestServer .100

 MPLS transport (AS 65000, 172.16.0.0/16)
   WAN-01<->WAN-02 Eth6      172.16.0.0/31
   WAN-01 Eth2 <-> Hub-01 Gi1  172.16.1.0/30 (PE .1, hub .2)
   WAN-02 Eth2 <-> Hub-02 Gi1  172.16.2.0/30 (PE .1, hub .2)
   PROVIDER_MPLS_PE segment  172.16.100.0/24  VRRP gw .1, WAN-01 .2, WAN-02 .3
                             Branch-01 .11, Branch-02 .12, Branch-03 .13
======================================================================
"""

import os
import re
import sys
import json
import time
import argparse
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

LOCK_RETRIES = 5
LOCK_BACKOFF = 2.0
PACE = 0.5
CONFIG_SET = os.getenv("EVE_CFSID", "default")   # EVE Pro startup-config set

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs", "arista")

MGMT_GW = "192.168.100.1"
ADMIN_SECRET = "admin"
VARP_MAC = "00:1c:73:00:00:99"
MLAG_SVI_MTU = 1500        # MLAG peer/iBGP SVIs (VLAN 4091-4094); 1500 behaves best in EVE-NG
SDWAN_ORG_NAME = "DSA-LAB"  # SD-WAN overlay organization name (used by controller/edge configs)

# ---------------------------------------------------------------------------
# FABRIC DATA MODEL
# ---------------------------------------------------------------------------
SPINE_ASN = 65100
LEAF_ASN = 65101
MPLS_ASN = 65000
HUB_SVC_ASN = 65200

SPINES = {
    "vEOS-Spine-01": {"mgmt": "192.168.100.20", "lo0": "10.0.250.1",
                      "links": {1: ("vEOS-Leaf-01", "Ethernet3", "10.0.1.0", "10.0.1.1"),
                                2: ("vEOS-Leaf-02", "Ethernet3", "10.0.1.4", "10.0.1.5")}},
    "vEOS-Spine-02": {"mgmt": "192.168.100.21", "lo0": "10.0.250.2",
                      "links": {1: ("vEOS-Leaf-01", "Ethernet4", "10.0.1.2", "10.0.1.3"),
                                2: ("vEOS-Leaf-02", "Ethernet4", "10.0.1.6", "10.0.1.7")}},
}

VLANS = {
    10:  {"name": "CONTROLLERS_VPN0",  "vrf": "WAN",     "vni": 10010, "net": "10.10.10",   "mask": 24},
    100: {"name": "HUB_SVC_TRANSIT",   "vrf": "SERVICE", "vni": 10100, "net": "10.100.100", "mask": 24},
    200: {"name": "HUB_SERVERS",       "vrf": "SERVICE", "vni": 10200, "net": "10.200.1",   "mask": 24},
}
VRFS = {"WAN": 50010, "SERVICE": 50020}

# MLAG SVIs: vlan -> (vrf or None, subnet base). Leaf-01 = .0, Leaf-02 = .1
MLAG_SVIS = {
    4094: (None, "10.255.252", "MLAG_PEER"),
    4093: (None, "10.255.251", "MLAG_iBGP_UNDERLAY"),
    4091: ("WAN", "10.255.253", "MLAG_iBGP_VRF_WAN"),
    4092: ("SERVICE", "10.255.254", "MLAG_iBGP_VRF_SERVICE"),
}

LEAVES = {
    "vEOS-Leaf-01": {
        "mgmt": "192.168.100.22", "lo0": "10.0.250.11", "lo1": "10.0.255.11",
        "mlag_side": 0, "peer": "vEOS-Leaf-02", "svi_host": 2,
        "uplinks": {3: ("vEOS-Spine-01", "Ethernet1", "10.0.1.1", "10.0.1.0"),
                    4: ("vEOS-Spine-02", "Ethernet1", "10.0.1.3", "10.0.1.2")},
        "access": {1: (10, "vManage-01_eth0_VPN0"), 2: (10, "vSmart-01_eth0_VPN0")},
        "hub_pos": {7: (7, "c8000v-Hub-01_Gi3"), 8: (8, "c8000v-Hub-02_Gi3")},
        "wan_link": (5, "vEOS-WAN-01", "Ethernet1", "10.0.2.0", "10.0.2.1"),
        "fw_link": (6, "DC1_FW1", "ethernet1/1", "10.0.3.1", "10.0.3.2", 30),
    },
    "vEOS-Leaf-02": {
        "mgmt": "192.168.100.23", "lo0": "10.0.250.12", "lo1": "10.0.255.11",
        "mlag_side": 1, "peer": "vEOS-Leaf-01", "svi_host": 3,
        "uplinks": {3: ("vEOS-Spine-01", "Ethernet2", "10.0.1.5", "10.0.1.4"),
                    4: ("vEOS-Spine-02", "Ethernet2", "10.0.1.7", "10.0.1.6")},
        "access": {1: (10, "vBond-01_ge0/0_VPN0"), 2: (200, "Host-TestServer_eth0")},
        "hub_pos": {7: (7, "c8000v-Hub-01_Gi4"), 8: (8, "c8000v-Hub-02_Gi4")},
        "wan_link": (5, "vEOS-WAN-02", "Ethernet1", "10.0.2.2", "10.0.2.3"),
        "fw_link": None,
    },
}
HUB_BGP_PEERS = {"10.100.100.11": "c8000v-Hub-01_SVC_VPN10", "10.100.100.12": "c8000v-Hub-02_SVC_VPN10"}

WAN_ROUTERS = {
    "vEOS-WAN-01": {
        "mgmt": "192.168.100.24", "lo0": "172.16.255.1", "vrrp_prio": 110,
        "leaf": (1, "vEOS-Leaf-01_Eth5", "10.0.2.1", "10.0.2.0"),
        "hub": (2, "c8000v-Hub-01_Gi1_MPLS", "172.16.1.1/30"),
        "cloud": (3, "172.16.100.2/24"),
        "peer": (6, "vEOS-WAN-02_Eth6", "172.16.0.0", "172.16.0.1"),
    },
    "vEOS-WAN-02": {
        "mgmt": "192.168.100.25", "lo0": "172.16.255.2", "vrrp_prio": 100,
        "leaf": (1, "vEOS-Leaf-02_Eth5", "10.0.2.3", "10.0.2.2"),
        "hub": (2, "c8000v-Hub-02_Gi1_MPLS", "172.16.2.1/30"),
        "cloud": (3, "172.16.100.3/24"),
        "peer": (6, "vEOS-WAN-01_Eth6", "172.16.0.1", "172.16.0.0"),
    },
}
MPLS_VRRP_VIP = "172.16.100.1"


# ---------------------------------------------------------------------------
# CONFIG RENDERING
# ---------------------------------------------------------------------------
def common_header(host, mgmt_ip):
    return f"""! device: {host}
! generated by build_arista_configs.py (TSK-05)
!
transceiver qsfp default-mode 4x10G
!
service routing protocols model multi-agent
!
hostname {host}
!
spanning-tree mode mstp
!
no aaa root
username admin privilege 15 role network-admin secret {ADMIN_SECRET}
!
vrf instance MGMT
!
interface Management1
   description OOB_Management
   vrf MGMT
   ip address {mgmt_ip}/24
!
ip route vrf MGMT 0.0.0.0/0 {MGMT_GW}
!
management api http-commands
   no shutdown
   !
   vrf MGMT
      no shutdown
!
management ssh
   vrf MGMT
      no shutdown
!
"""


def routed_intf(num, desc, ip, mask=31, vrf=None, mtu=9214):
    lines = [f"interface Ethernet{num}", f"   description {desc}", "   no switchport"]
    if mtu:
        lines.append(f"   mtu {mtu}")
    if vrf:
        lines.append(f"   vrf {vrf}")
    lines.append(f"   ip address {ip}/{mask}")
    return "\n".join(lines) + "\n!\n"


def loopback_policy():
    return """ip prefix-list PL-LOOPBACKS
   seq 10 permit 10.0.250.0/24 eq 32
   seq 20 permit 10.0.255.0/24 eq 32
!
route-map RM-CONN-2-BGP permit 10
   match ip address prefix-list PL-LOOPBACKS
!
"""


def render_spine(host, d):
    c = common_header(host, d["mgmt"])
    for num, (peer, pif, my_ip, _peer_ip) in d["links"].items():
        c += routed_intf(num, f"P2P_{peer}_{pif}", my_ip)
    c += """interface Ethernet10
   description RESERVED_SPINE_INTERLINK_unused_in_EVPN
   shutdown
!
interface Ethernet11
   description RESERVED_SPINE_INTERLINK_unused_in_EVPN
   shutdown
!
"""
    c += f"interface Loopback0\n   description ROUTER_ID_EVPN\n   ip address {d['lo0']}/32\n!\n"
    c += "ip routing\nno ip routing vrf MGMT\n!\n"
    c += loopback_policy()
    c += f"""router bgp {SPINE_ASN}
   router-id {d['lo0']}
   no bgp default ipv4-unicast
   maximum-paths 4 ecmp 4
   neighbor UNDERLAY peer group
   neighbor UNDERLAY remote-as {LEAF_ASN}
   neighbor UNDERLAY send-community
   neighbor UNDERLAY maximum-routes 12000
   neighbor EVPN-OVERLAY peer group
   neighbor EVPN-OVERLAY remote-as {LEAF_ASN}
   neighbor EVPN-OVERLAY next-hop-unchanged
   neighbor EVPN-OVERLAY update-source Loopback0
   neighbor EVPN-OVERLAY bfd
   neighbor EVPN-OVERLAY ebgp-multihop 3
   neighbor EVPN-OVERLAY send-community
   neighbor EVPN-OVERLAY maximum-routes 0
"""
    for num, (peer, _pif, _my_ip, peer_ip) in d["links"].items():
        c += f"   neighbor {peer_ip} peer group UNDERLAY\n"
        c += f"   neighbor {peer_ip} description {peer}_underlay\n"
    for leaf, ld in LEAVES.items():
        c += f"   neighbor {ld['lo0']} peer group EVPN-OVERLAY\n"
        c += f"   neighbor {ld['lo0']} description {leaf}_evpn\n"
    c += """   redistribute connected route-map RM-CONN-2-BGP
   !
   address-family evpn
      neighbor EVPN-OVERLAY activate
   !
   address-family ipv4
      neighbor UNDERLAY activate
!
end
"""
    return c


def render_leaf(host, d):
    side = d["mlag_side"]
    peer_side = 1 - side
    host_octet = d["svi_host"]
    c = common_header(host, d["mgmt"])

    # VLANs
    for vid, v in VLANS.items():
        c += f"vlan {vid}\n   name {v['name']}\n!\n"
    for vid, (_vrf, _net, name) in sorted(MLAG_SVIS.items()):
        c += f"vlan {vid}\n   name {name}\n   trunk group MLAG_PEER\n!\n"
    c += "no spanning-tree vlan-id 4091-4094\n!\n"

    # VRFs
    for vrf in VRFS:
        c += f"vrf instance {vrf}\n!\n"

    # MLAG peer link
    c += f"""interface Port-Channel1000
   description MLAG_PEER_{d['peer']}
   switchport mode trunk
   switchport trunk group MLAG_PEER
!
interface Ethernet10
   description MLAG_PEER_{d['peer']}_Eth10
   channel-group 1000 mode active
!
interface Ethernet11
   description MLAG_PEER_{d['peer']}_Eth11
   channel-group 1000 mode active
!
"""
    # Access ports (controllers / servers)
    for num, (vid, desc) in d["access"].items():
        c += f"""interface Ethernet{num}
   description {desc}
   switchport access vlan {vid}
   spanning-tree portfast
!
"""
    # Spine uplinks
    for num, (spine, pif, my_ip, _peer_ip) in d["uplinks"].items():
        c += routed_intf(num, f"P2P_{spine}_{pif}", my_ip)

    # WAN router (MPLS PE) link in VRF WAN
    num, wan, wif, my_ip, _ = d["wan_link"]
    c += routed_intf(num, f"P2P_{wan}_{wif}_VRF-WAN", my_ip, vrf="WAN", mtu=1500)

    # DC1_FW1 link in VRF WAN (Leaf-01 only)
    if d["fw_link"]:
        num, fw, fif, my_ip, _fw_ip, mask = d["fw_link"]
        c += routed_intf(num, f"{fw}_{fif}_VRF-WAN_INTERNET", my_ip, mask=mask, vrf="WAN", mtu=1500)

    # Hub c8000v LACP port-channels (MLAG)
    for num, (po, desc) in d["hub_pos"].items():
        c += f"""interface Ethernet{num}
   description {desc}
   channel-group {po} mode active
!
interface Port-Channel{po}
   description {desc.rsplit('_', 1)[0]}_Po1_SVC_VPN10
   switchport access vlan 100
   mlag {po}
   spanning-tree portfast
!
"""
    # Loopbacks
    c += f"""interface Loopback0
   description ROUTER_ID_EVPN
   ip address {d['lo0']}/32
!
interface Loopback1
   description VTEP_SHARED_MLAG
   ip address {d['lo1']}/32
!
"""
    # MLAG / iBGP SVIs
    for vid, (vrf, net, name) in sorted(MLAG_SVIS.items()):
        c += f"interface Vlan{vid}\n   description {name}\n"
        if vid == 4094:
            c += "   no autostate\n"
        c += f"   mtu {MLAG_SVI_MTU}\n"
        if vrf:
            c += f"   vrf {vrf}\n"
        c += f"   ip address {net}.{side}/31\n!\n"

    # Tenant SVIs with VARP gateway
    for vid, v in VLANS.items():
        c += f"""interface Vlan{vid}
   description {v['name']}
   vrf {v['vrf']}
   ip address {v['net']}.{host_octet}/{v['mask']}
   ip virtual-router address {v['net']}.1
!
"""
    # VXLAN
    c += "interface Vxlan1\n   description LEAF_PAIR_01_VTEP\n"
    c += "   vxlan source-interface Loopback1\n"
    c += "   vxlan virtual-router encapsulation mac-address mlag-system-id\n"
    c += "   vxlan udp-port 4789\n"
    for vid, v in VLANS.items():
        c += f"   vxlan vlan {vid} vni {v['vni']}\n"
    for vrf, vni in VRFS.items():
        c += f"   vxlan vrf {vrf} vni {vni}\n"
    c += "!\n"

    c += f"ip virtual-router mac-address {VARP_MAC}\n!\n"
    c += "ip routing\nno ip routing vrf MGMT\n"
    for vrf in VRFS:
        c += f"ip routing vrf {vrf}\n"
    c += "!\n"

    if d["fw_link"]:
        fw_ip = d["fw_link"][4]
        c += f"ip route vrf WAN 0.0.0.0/0 {fw_ip} name DEFAULT_TO_DC1_FW1\n!\n"

    # MLAG
    c += f"""mlag configuration
   domain-id LEAF_PAIR_01
   local-interface Vlan4094
   peer-address 10.255.252.{peer_side}
   peer-link Port-Channel1000
   reload-delay mlag 300
   reload-delay non-mlag 330
!
"""
    c += loopback_policy()
    c += """ip prefix-list PL-CONTROLLERS
   seq 10 permit 10.10.10.0/24
!
route-map RM-VRF-WAN-TO-MPLS permit 10
   description Only advertise the controller subnet into MPLS (no default/internet)
   match ip address prefix-list PL-CONTROLLERS
!
"""
    # BGP
    wan_peer_ip = d["wan_link"][4]
    wan_name = d["wan_link"][1]
    c += f"""router bgp {LEAF_ASN}
   router-id {d['lo0']}
   no bgp default ipv4-unicast
   maximum-paths 4 ecmp 4
   neighbor UNDERLAY peer group
   neighbor UNDERLAY remote-as {SPINE_ASN}
   neighbor UNDERLAY send-community
   neighbor UNDERLAY maximum-routes 12000
   neighbor MLAG-IPV4-PEER peer group
   neighbor MLAG-IPV4-PEER remote-as {LEAF_ASN}
   neighbor MLAG-IPV4-PEER next-hop-self
   neighbor MLAG-IPV4-PEER description {d['peer']}_MLAG
   neighbor MLAG-IPV4-PEER send-community
   neighbor MLAG-IPV4-PEER maximum-routes 12000
   neighbor EVPN-OVERLAY peer group
   neighbor EVPN-OVERLAY remote-as {SPINE_ASN}
   neighbor EVPN-OVERLAY update-source Loopback0
   neighbor EVPN-OVERLAY bfd
   neighbor EVPN-OVERLAY ebgp-multihop 3
   neighbor EVPN-OVERLAY send-community
   neighbor EVPN-OVERLAY maximum-routes 0
"""
    for _num, (spine, _pif, _my_ip, peer_ip) in d["uplinks"].items():
        c += f"   neighbor {peer_ip} peer group UNDERLAY\n"
        c += f"   neighbor {peer_ip} description {spine}_underlay\n"
    c += f"   neighbor 10.255.251.{peer_side} peer group MLAG-IPV4-PEER\n"
    for spine, sd in SPINES.items():
        c += f"   neighbor {sd['lo0']} peer group EVPN-OVERLAY\n"
        c += f"   neighbor {sd['lo0']} description {spine}_evpn\n"
    c += "   redistribute connected route-map RM-CONN-2-BGP\n   !\n"

    for vid, v in VLANS.items():
        c += f"""   vlan {vid}
      rd {d['lo0']}:{v['vni']}
      route-target both {v['vni']}:{v['vni']}
      redistribute learned
   !
"""
    c += """   address-family evpn
      neighbor EVPN-OVERLAY activate
   !
   address-family ipv4
      neighbor UNDERLAY activate
      neighbor MLAG-IPV4-PEER activate
   !
"""
    # VRF WAN
    c += f"""   vrf WAN
      rd {d['lo0']}:{VRFS['WAN']}
      route-target import evpn {VRFS['WAN']}:{VRFS['WAN']}
      route-target export evpn {VRFS['WAN']}:{VRFS['WAN']}
      router-id {d['lo0']}
      neighbor 10.255.253.{peer_side} peer group MLAG-IPV4-PEER
      neighbor {wan_peer_ip} remote-as {MPLS_ASN}
      neighbor {wan_peer_ip} description {wan_name}_MPLS_PE
      neighbor {wan_peer_ip} route-map RM-VRF-WAN-TO-MPLS out
      neighbor {wan_peer_ip} send-community
      redistribute connected
      redistribute static
      !
      address-family ipv4
         neighbor 10.255.253.{peer_side} activate
         neighbor {wan_peer_ip} activate
   !
"""
    # VRF SERVICE
    c += f"""   vrf SERVICE
      rd {d['lo0']}:{VRFS['SERVICE']}
      route-target import evpn {VRFS['SERVICE']}:{VRFS['SERVICE']}
      route-target export evpn {VRFS['SERVICE']}:{VRFS['SERVICE']}
      router-id {d['lo0']}
      neighbor 10.255.254.{peer_side} peer group MLAG-IPV4-PEER
"""
    for ip, desc in HUB_BGP_PEERS.items():
        c += f"      neighbor {ip} remote-as {HUB_SVC_ASN}\n"
        c += f"      neighbor {ip} description {desc}\n"
        c += f"      neighbor {ip} send-community\n"
    c += "      redistribute connected\n      !\n      address-family ipv4\n"
    c += f"         neighbor 10.255.254.{peer_side} activate\n"
    for ip in HUB_BGP_PEERS:
        c += f"         neighbor {ip} activate\n"
    c += "!\nend\n"
    return c


def render_wan(host, d):
    c = common_header(host, d["mgmt"])
    num, desc, my_ip, _ = d["leaf"]
    c += routed_intf(num, f"P2P_{desc}_DC_CONTROLLERS", my_ip, mtu=1500)
    num, desc, cidr = d["hub"]
    ip, mask = cidr.split("/")
    c += routed_intf(num, desc, ip, mask=mask, mtu=1500)
    num, cidr = d["cloud"]
    ip, mask = cidr.split("/")
    c += f"""interface Ethernet{num}
   description PROVIDER_MPLS_PE_BRANCH_SEGMENT
   no switchport
   ip address {ip}/{mask}
   vrrp 1 priority-level {d['vrrp_prio']}
   vrrp 1 ipv4 {MPLS_VRRP_VIP}
!
"""
    num, desc, my_ip, _ = d["peer"]
    c += routed_intf(num, f"P2P_{desc}_iBGP", my_ip, mtu=1500)
    c += f"interface Loopback0\n   description ROUTER_ID\n   ip address {d['lo0']}/32\n!\n"
    c += "ip routing\nno ip routing vrf MGMT\n!\n"

    leaf_peer = d["leaf"][3]
    leaf_name = d["leaf"][1].split("_")[0]
    wan_peer = d["peer"][3]
    peer_name = d["peer"][1].split("_")[0]
    c += f"""router bgp {MPLS_ASN}
   router-id {d['lo0']}
   no bgp default ipv4-unicast
   maximum-paths 2
   neighbor {leaf_peer} remote-as {LEAF_ASN}
   neighbor {leaf_peer} description {leaf_name}_VRF_WAN_controllers
   neighbor {leaf_peer} send-community
   neighbor {wan_peer} remote-as {MPLS_ASN}
   neighbor {wan_peer} description {peer_name}_iBGP
   neighbor {wan_peer} next-hop-self
   neighbor {wan_peer} send-community
   redistribute connected
   !
   address-family ipv4
      neighbor {leaf_peer} activate
      neighbor {wan_peer} activate
!
end
"""
    return c


def render_all():
    configs = {}
    for h, d in SPINES.items():
        configs[h] = render_spine(h, d)
    for h, d in LEAVES.items():
        configs[h] = render_leaf(h, d)
    for h, d in WAN_ROUTERS.items():
        configs[h] = render_wan(h, d)
    return configs


def write_files(configs):
    os.makedirs(OUT_DIR, exist_ok=True)
    for host, text in configs.items():
        path = os.path.join(OUT_DIR, f"{host}.cfg")
        with open(path, "w") as f:
            f.write(text)
    print(f"[+] Wrote {len(configs)} configs to {OUT_DIR}")


# ---------------------------------------------------------------------------
# EVE-NG PRO API
# ---------------------------------------------------------------------------
class EveCrash(Exception):
    """EVE returned an HTML error page instead of JSON (PHP error/500)."""


def short(text, limit=300):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_body(text):
    """EVE Pro 7.x can wrap its JSON with a PHP print_r() dump; extract the JSON object."""
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
    url = f"{BASE_URL}{path}"
    delay = LOCK_BACKOFF
    body = {}
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
    ok, body = api("GET", f"/labs{LAB_PATH}/nodes", label="list nodes")
    if not ok:
        print(f"[-] Failed to get nodes: {body}")
        sys.exit(1)
    return body.get("data", {}) or {}


def inject_config(node_id, node_name, config_text):
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
    parser = argparse.ArgumentParser(description="Build and inject Arista EVPN fabric startup configs")
    parser.add_argument("--dry-run", action="store_true", help="render config files only")
    args = parser.parse_args()

    print("=" * 65)
    print("   ARISTA vEOS EVPN/VXLAN FABRIC + MPLS WAN CONFIG BUILDER   ")
    print("=" * 65)

    configs = render_all()
    write_files(configs)
    if args.dry_run:
        return

    eve_login()
    try:
        name_to_id = {info["name"]: nid for nid, info in get_nodes().items()}
        results = {}
        for host, text in configs.items():
            node_id = name_to_id.get(host)
            if not node_id:
                print(f"[!] Node '{host}' not found in lab. Skipping.")
                results[host] = False
                continue
            results[host] = inject_config(node_id, host, text)

        done = sum(results.values())
        print(f"\n[*] {done}/{len(configs)} vEOS nodes configured.")
        print("[*] Stop + Wipe the vEOS nodes in EVE, then start them so they boot these configs.")
    except EveCrash as err:
        print(f"\n[-] EVE returned an error page, stopping:\n    {err}")
        sys.exit(2)
    finally:
        eve_logout()


if __name__ == "__main__":
    main()