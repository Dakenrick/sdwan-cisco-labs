# Cisco Catalyst SD-WAN AAR & EAAR Implementation Workbook

---

## 1. Task Tracking & Implementation Checklist

Below is the master task tracking matrix for setting up the lab, automating configurations, deploying policies, and executing tests.

| Task ID | Module / Area | Description | Target Environment | Status | Notes / Script Ref |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **TSK-01** | **Lab Infra** | Provision EVE-NG VM & import Catalyst SD-WAN 20.18.4+ QCOW2 images (`c8000v`) | ESXi / EVE-NG | ⬜ Pending | Manager, Controller, Validator, c8000v |
| **TSK-02** | **Lab Infra** | Deploy initial baseline topology (1 Manager, 1 Controller, 1 Validator, 2 Hub c8000v, 2 Branch c8000v) | EVE-NG | ⬜ Pending | Phase 1 baseline pod |
| **TSK-03** | **Automation** | Set up Python/Ansible environment on Ubuntu VM with VS Code integration | Ubuntu VM | ⬜ Pending | Python virtualenv + `cisco.sdwan` |
| **TSK-04** | **Automation** | Build automated certificate generation script (ACME/Certbot or enterprise CA) for controller onboarding | Ubuntu VM / Scripts | ⬜ Pending | Automate vManage/vSmart cert sign |
| **TSK-05** | **Switching** | Deploy Arista vEOS BGP EVPN/VXLAN fabric pairs at Hub (Spines, Compute Leafs, Border Leafs, WAN Leafs) | EVE-NG Hub | ⬜ Pending | Multi-leaf pair architecture |
| **TSK-06** | **Visibility** | Enable `app-visibility` on remote c8000v LAN/service-side interfaces | Branch c8000v | ⬜ Pending | Localized policy push |
| **TSK-07** | **Traffic Gen** | Build Linux Traffic Generator VM (Docker/Python script with `iperf3`, `scapy`, `curl`) | EVE-NG Branch | ⬜ Pending | Multi-class synthetic generator |
| **TSK-08** | **Policy** | Define Centralized SLA Classes (`SLA_VOICE_VIDEO`, `SLA_CRITICAL_DATA`, `SLA_CLOUD_SAAS`, `SLA_BULK`) | vManage / vSmart | ⬜ Pending | Tuned for internet & MPLS |
| **TSK-09** | **Policy** | Configure AAR and EAAR match conditions & `preferred-color` / `fallback-to-best-path` steering | vManage / vSmart | ⬜ Pending | Match DSCP, L4 Ports, NBAR2 |
| **TSK-10** | **Testing** | Execute Baseline Test 1 (Unimpaired path traffic steering) | EVE-NG | ⬜ Pending | Confirm traffic on primary colors |
| **TSK-11** | **Testing** | Execute Failover Test 2 (Impairment injection via EVE-NG API / `tc netem`) | EVE-NG | ⬜ Pending | Latency > 150ms / Loss > 1% |
| **TSK-12** | **Testing** | Execute Recovery Test 3 (Impairment removal & failback verification) | EVE-NG | ⬜ Pending | Session stability & state restore |

---

## 2. Traffic Generator Setup (Linux Node)

To test AAR/EAAR accurately, synthetic traffic is generated to match specific Layer 4 ports, DSCP tags, and NBAR signatures.

### Linux Traffic Generator Script (`traffic_gen.py`)

```python
#!/usr/bin/env python3
"""
Synthetic Traffic Generator for Cisco SD-WAN AAR Testing
Generates Voice (DSCP EF), Critical Data (DSCP AF21), SaaS (HTTP/HTTPS), and Bulk (iperf3).
"""

import time
import subprocess
from scapy.all import IP, UDP, send, Raw

HUB_SERVER_IP = "10.200.1.100"  # Target Server behind Hub Leaf
INTERFACE = "eth1"              # LAN interface connected to c8000v

def send_voice_traffic():
    """Generates RTP-like Voice Traffic (DSCP EF / 46)"""
    print("[+] Sending Synthetic Voice Stream (DSCP EF / Port 16384)...")
    pkt = IP(dst=HUB_SERVER_IP, tos=184)/UDP(sport=5060, dport=16384)/Raw(load=b"VOICE_PAYLOAD"*10)
    send(pkt, count=50, inter=0.02, verbose=False) # 20ms packet intervals

def send_critical_data():
    """Generates ERP/Database Traffic (DSCP AF21 / 18)"""
    print("[+] Sending Mission Critical Data Stream (DSCP AF21 / Port 1521)...")
    pkt = IP(dst=HUB_SERVER_IP, tos=72)/UDP(sport=49152, dport=1521)/Raw(load=b"ORACLE_DB_DATA"*20)
    send(pkt, count=20, inter=0.05, verbose=False)

def run_bulk_transfer():
    """Triggers Bulk Data Transfer via iperf3"""
    print("[+] Triggering Bulk Transfer (iperf3)...")
    subprocess.Popen(["iperf3", "-c", HUB_SERVER_IP, "-t", "10", "-b", "5M"])

if __name__ == "__main__":
    print("--- Starting SD-WAN AAR Synthetic Traffic Generator ---")
    while True:
        send_voice_traffic()
        send_critical_data()
        run_bulk_transfer()
        time.sleep(5)
```

---

## 3. Architecture Answers & Engineering Strategy

### A. App-Visibility Deployment Scope
* **Where to turn it on?** Enable `app-visibility` on the **remote site (branch) service-side interfaces**. 
* **Why?** Classification for AAR must happen at the **ingress point** of the WAN overlay. Capturing it on the branch service interface allows the local c8000v to match traffic on NBAR2 or DSCP before encapsulating it into the IPSec overlay tunnel.
* **Hub Scope:** Enable visibility on Hub service interfaces primarily for cross-site traffic hairpinning through the hub.

### B. Is Enhanced AAR (EAAR) the only way to use App-Visibility?
* **No.** Traditional AAR supports standard NBAR2 `app-list` matching inside Centralized Data Policies.
* **What EAAR adds (20.18.4+):** EAAR integrates application visibility directly into **Policy Groups**, dynamic app categories, and advanced telemetry rules. It simplifies managing L7 policies without writing complex sequence rules for every custom application.

### C. Transport Selection Strategy per Traffic Class (Centralized Internet Breakout)

| Traffic Class | Primary Transport Target | Secondary (Backup) Transport | Justification |
| :--- | :--- | :--- | :--- |
| **Voice / Video** | `mpls` | `biz-internet` | MPLS guarantees zero jitter and low loss. Internet acts as failover with FEC enabled. |
| **Mission-Critical** | `biz-internet` | `mpls` | Save MPLS bandwidth for latency-sensitive voice; route TCP ERP apps over reliable broadband. |
| **Cloud / SaaS** | `biz-internet` (Hub Exit) | `public-internet` (Hub Exit) | Backhaul via overlay to Hub Firewall; exit centralized internet gateway. |
| **Bulk Data** | `public-internet` | `lte` / Lowest Priority | Bulk backups utilize available broadband capacity without impacting MPLS/SLA paths. |

---

## 4. End-to-End Enterprise Architecture Topology

### Centralized Internet & Firewall Architecture Diagram

```
       ┌───────────────────────────────┐     ┌───────────────────────────────────┐
       │   Enterprise Hub Servers      │     │  Cisco Catalyst SD-WAN Controllers│
       │  (DNS, DCs, PKI/Cert Gen,     │     │   (SD-WAN Manager, Controller,    │
       │   Traffic Test Destinations)  │     │            Validator)             │
       └───────────────┬───────────────┘     └─────────────────┬─────────────────┘
                       │                                       │
                       └───────────────────┬───────────────────┘
                                           │
                                           ▼
            ┌─────────────────────────────────────────────────────────────┐
            │            Arista vEOS BGP EVPN / VXLAN Fabric              │
            │  ┌─────────────────┐ ┌────────────────┐ ┌────────────────┐ │
            │  │  Compute Leafs  │ │  Spine Nodes   │ │  Border Leafs  │ │
            │  └─────────────────┘ └────────────────┘ └────────────────┘ │
            └───────────────┬──────────────────────────────┬──────────────┘
                            │                              │
                            │ eBGP                         │ eBGP
                            ▼                              ▼
            ┌───────────────────────────────┐   ┌─────────────────────────┐
            │ SD-WAN Hub Sites (Redundant)  │   │   Centralized Firewall  │
            │(Hub Site 1 & 2 Dual c8000v)   ├──►│ (Palo Alto Gateway for  │──► Public Internet
            └───────────────┬───────────────┘   │  Centralized Breakout)  │
                            │                   └─────────────────────────┘
                            │
                    ┌───────┴─────────────────────────────────────┐
                    │                                             │
                    ▼                                             ▼
┌──────────────────────────────────────┐       ┌──────────────────────────────────────┐
│     Private Transport Network        │       │    Commercial Internet Transports    │
│ (Arista WAN Routers/Switches MPLS)   │       │   (Commercial ISP 1 & ISP 2 Tunnels) │
└───────────────────┬──────────────────┘       └──────────────────┬───────────────────┘
                    │                                             │
                    │                                             │
                    └──────────────────────┬──────────────────────┘
                                           │ SD-WAN IPSec Overlay Tunnels
                                           ▼
            ┌─────────────────────────────────────────────────────────────┐
            │                    Branch Sites (15 Sites)                  │
            │         (Branch c8000v WAN Edges - Single & Dual)           │
            └──────────────────────────────┬──────────────────────────────┘
                                           │ Service VPN / LAN
                                           ▼
                                   ┌───────────────┐
                                   │   End Users   │
                                   │  (Workstations│
                                   │   & Voice/VDI)│
                                   └───────────────┘
```

---

## 5. VS Code Integration Strategy

To work directly within Visual Studio Code on your Linux Ubuntu VM:

1. **Remote SSH Extension:** Connect VS Code directly to your Ubuntu VM on ESXi (`Host ubuntu-vm`).
2. **Git Version Control:** Clone your GitHub repository inside VS Code to manage your Ansible playbooks, Terraform scripts, and Python traffic tools.
3. **Markdown Tools:** Install **Markdown Preview Enhanced** and **Markdown PDF** in VS Code to view and export this file directly to PDF or Word.