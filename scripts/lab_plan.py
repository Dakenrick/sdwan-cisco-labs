#!/usr/bin/env python3
"""
DSA-LAB shared addressing plan (single source of truth for all build scripts)
Directory: /home/dakenrick/Documents/EVE-NG/sdwan-cisco-labs/scripts/lab_plan.py

Arista fabric addressing lives in build_arista_configs.py (TSK-05); the values
below are the SD-WAN side and must stay consistent with it:
  - Controllers sit on Leaf VLAN 10 / VRF WAN  10.10.10.0/24, gateway .1 (VARP)
  - MPLS transport 172.16.0.0/16 via vEOS-WAN-01/02
  - Internet transport = COMMERCIAL_INTERNET_WAN bridge (simulated public 203.0.113.0/24)
"""

import os

# ---------------------------------------------------------------- overlay
ORG_NAME = "DSA-LAB"
VBOND_PORT = 12346

# ---------------------------------------------------------------- OOB mgmt (VPN 512)
# Lab devices use ONLY 192.168.100.20 and up (.1-.19 belong to the home network).
#   .20-.25  Arista vEOS (Spine-01/02, Leaf-01/02, WAN-01/02)
#   .26-.28  vManage-01, vSmart-01, vBond-01
#   .29-.33  c8000v Hub-01/02, Branch-01/02/03     (planned)
#   .34-.36  DC1_FW1, FW-Branch-01/02 mgmt          (planned)
#   .37-.40  Host-TestServer, Host-Branch-01/02/03  (planned)
MGMT_NET = "192.168.100"
MGMT_MASK = 24
MGMT_GW = "192.168.100.1"
MGMT_FIRST_HOST = 20

# ---------------------------------------------------------------- controller VPN 0
CTRL_NET = "10.10.10"
CTRL_MASK = 24
CTRL_GW = "10.10.10.1"           # Leaf-01/02 VARP gateway (VLAN 10, VRF WAN)
CONTROLLER_SITE_ID = 1000

# Simulated public internet (COMMERCIAL_INTERNET_WAN). DC1_FW1 will 1:1 NAT the
# controllers to these addresses so internet-color edges can reach them.
INET_NET = "203.0.113"
INET_MASK = 24

CONTROLLERS = {
    "vManage-01": {
        "personality": "vmanage", "system_ip": "1.1.1.1",
        "vpn0_if": "eth0", "vpn0_ip": f"{CTRL_NET}.10",
        "mgmt_if": "eth2", "mgmt_ip": f"{MGMT_NET}.26",
        "public_ip": f"{INET_NET}.10",
        "unused_ifs": ["eth1"],          # reserved for future DR
    },
    "vSmart-01": {
        "personality": "vsmart", "system_ip": "1.1.1.2",
        "vpn0_if": "eth0", "vpn0_ip": f"{CTRL_NET}.20",
        "mgmt_if": "eth1", "mgmt_ip": f"{MGMT_NET}.27",
        "public_ip": f"{INET_NET}.20",
        "unused_ifs": [],
    },
    "vBond-01": {
        "personality": "vbond", "system_ip": "1.1.1.3",
        "vpn0_if": "ge0/0", "vpn0_ip": f"{CTRL_NET}.30",
        "mgmt_if": "eth0", "mgmt_ip": f"{MGMT_NET}.28",
        "public_ip": f"{INET_NET}.30",
        "unused_ifs": [],
        # 20.18.5 vBond ships with eth0 in VPN 0 (dhcp-client); eth0 is our VPN 512 mgmt
        "remove_vpn0_ifs": ["eth0"],
    },
}

VBOND_PRIVATE_IP = CONTROLLERS["vBond-01"]["vpn0_ip"]
VBOND_PUBLIC_IP = CONTROLLERS["vBond-01"]["public_ip"]
VMANAGE_MGMT_IP = CONTROLLERS["vManage-01"]["mgmt_ip"]

# ---------------------------------------------------------------- edges (planned)
# Hub-01   system-ip 1.1.10.1  site 100   MPLS 172.16.1.2/30  -> 172.16.1.1
# Hub-02   system-ip 1.1.10.2  site 100   MPLS 172.16.2.2/30  -> 172.16.2.1
# Branch-01 system-ip 1.1.20.1 site 201   MPLS 172.16.100.11  -> 172.16.100.1
# Branch-02 system-ip 1.1.20.2 site 202   MPLS 172.16.100.12  -> 172.16.100.1
# Branch-03 system-ip 1.1.20.3 site 203   MPLS 172.16.100.13  -> 172.16.100.1 (MPLS only)

# ---------------------------------------------------------------- credentials
SDWAN_USER = os.getenv("SDWAN_USER", "admin")
SDWAN_PASS = os.getenv("SDWAN_PASS", "admin")

# Guard: every lab OOB address must be .20 or higher
for _n, _c in CONTROLLERS.items():
    assert int(_c["mgmt_ip"].rsplit(".", 1)[1]) >= MGMT_FIRST_HOST, f"{_n} mgmt IP below .{MGMT_FIRST_HOST}"
