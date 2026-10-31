#!/usr/bin/env python3
"""
Arista vEOS EVPN/VXLAN & MLAG Config Generator
Directory: /home/dakenrick/Documents/EVE-NG/sdwan-cisco-labs/scripts/build_arista_configs.py
"""

import os
from jinja2 import Environment, FileSystemLoader

WORKSPACE = "/home/dakenrick/Documents/EVE-NG/sdwan-cisco-labs"
TEMPLATE_DIR = f"{WORKSPACE}/templates"
OUTPUT_DIR = f"{WORKSPACE}/configs/arista"

os.makedirs(OUTPUT_DIR, exist_ok=True)

env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
leaf_template = env.get_template("arista_leaf.j2")

# Fabric Node Definitions (Single ASN for Arista Fabric Pair)
leaf_nodes = [
    {
        "hostname": "vEOS-Leaf-01",
        "asn": 65001,
        "site_id": 100,
        "mlag_vlan": 4094,
        "mlag_ip": "10.255.255.1",
        "mlag_peer_ip": "10.255.255.2",
        "loopback_ip": "10.1.0.1",
        "controller_vlan": 10,
        "spine1_link_ip": "10.0.1.2",
        "spine2_link_ip": "10.0.2.2",
        "spine1_ip": "10.1.0.101",
        "spine2_ip": "10.1.0.102",
    },
    {
        "hostname": "vEOS-Leaf-02",
        "asn": 65001,
        "site_id": 100,
        "mlag_vlan": 4094,
        "mlag_ip": "10.255.255.2",
        "mlag_peer_ip": "10.255.255.1",
        "loopback_ip": "10.1.0.2",
        "controller_vlan": 10,
        "spine1_link_ip": "10.0.3.2",
        "spine2_link_ip": "10.0.4.2",
        "spine1_ip": "10.1.0.101",
        "spine2_ip": "10.1.0.102",
    }
]

def generate_configs():
    print("=" * 65)
    print("      ARISTA vEOS EVPN/VXLAN CONFIGURATION GENERATOR      ")
    print("=" * 65)
    
    for leaf in leaf_nodes:
        config_output = leaf_template.render(leaf)
        file_path = f"{OUTPUT_DIR}/{leaf['hostname']}.cfg"
        with open(file_path, "w") as f:
            f.write(config_output)
        print(f"[+] Generated: {file_path}")

if __name__ == "__main__":
    generate_configs()