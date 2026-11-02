#!/usr/bin/env python3
"""
Cisco Catalyst SD-WAN Controller Startup-Config Builder & Injector
Directory: /home/dakenrick/Documents/EVE-NG/sdwan-cisco-labs/scripts/build_sdwan_controller_configs.py

Renders Day-0 configs for vManage-01, vSmart-01 and vBond-01 (org DSA-LAB) and
pushes them to EVE-NG Pro as startup configs.

  VPN 0   : controller transport on Leaf VLAN 10 (10.10.10.0/24), static default -> 10.10.10.1
  VPN 512 : OOB management on Cloud1 (192.168.100.0/24),           static default -> 192.168.100.1

Usage:
  python3 build_sdwan_controller_configs.py            # render to ./configs/sdwan/ + inject
  python3 build_sdwan_controller_configs.py --dry-run  # render files only

Startup configs apply on first boot only: Stop + Wipe the controllers, then start them.
"""

import os
import argparse

import lab_plan as plan
from eve_api import inject_many

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs", "sdwan")
TIMEZONE = "America/New_York"


def render_controller(host, c):
    is_vbond = c["personality"] == "vbond"
    vbond_line = (f" vbond {plan.VBOND_PRIVATE_IP} local" if is_vbond
                  else f" vbond {plan.VBOND_PRIVATE_IP}")
    remove_ifs = "".join(f" no interface {i}\n" for i in c.get("remove_vpn0_ifs", []))

    cfg = f"""system
 host-name               {host}
 system-ip               {c['system_ip']}
 site-id                 {plan.CONTROLLER_SITE_ID}
 organization-name       {plan.ORG_NAME}
 clock timezone {TIMEZONE}
{vbond_line}
!
vpn 0
{remove_ifs} interface {c['vpn0_if']}
  description CONTROLLER_TRANSPORT_LEAF_VLAN10
  ip address {c['vpn0_ip']}/{plan.CTRL_MASK}
  tunnel-interface
"""
    if is_vbond:
        cfg += "   encapsulation ipsec\n"
    cfg += f"""   allow-service all
  !
  no shutdown
 !
 ip route 0.0.0.0/0 {plan.CTRL_GW}
!
vpn 512
 interface {c['mgmt_if']}
  description OOB_MGMT_CLOUD1
  ip address {c['mgmt_ip']}/{plan.MGMT_MASK}
  no shutdown
 !
 ip route 0.0.0.0/0 {plan.MGMT_GW}
!
"""
    # c["unused_ifs"] (vManage eth1, reserved for DR) is intentionally left
    # unconfigured; unconfigured Viptela interfaces stay down.
    return cfg


def render_all():
    return {host: render_controller(host, c) for host, c in plan.CONTROLLERS.items()}


def main():
    parser = argparse.ArgumentParser(description="Build and inject SD-WAN controller startup configs")
    parser.add_argument("--dry-run", action="store_true", help="render config files only")
    args = parser.parse_args()

    print("=" * 65)
    print(f"   SD-WAN CONTROLLER DAY-0 CONFIG BUILDER  (org {plan.ORG_NAME})   ")
    print("=" * 65)

    configs = render_all()
    os.makedirs(OUT_DIR, exist_ok=True)
    for host, text in configs.items():
        with open(os.path.join(OUT_DIR, f"{host}.cfg"), "w") as f:
            f.write(text)
    print(f"[+] Wrote {len(configs)} configs to {OUT_DIR}")
    if args.dry_run:
        return

    results = inject_many(configs)
    print(f"\n[*] {sum(results.values())}/{len(configs)} controllers configured.")
    print("[*] Stop + Wipe the controllers in EVE, then start them (vManage first).")


if __name__ == "__main__":
    main()
