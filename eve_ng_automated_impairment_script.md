#!/usr/bin/env python3
"""
EVE-NG / Linux Traffic Impairment Automation Script
Uses SSH / Linux 'tc netem' to inject packet loss, latency, and jitter into WAN links for AAR testing.
"""

import paramiko
import sys
import time

# Router WAN Interface Configuration
WAN_ROUTER_IP = "192.168.1.50"  # Target cEdge or WAN Impairment Node IP
USERNAME = "admin"
PASSWORD = "Password123!"
INTERFACE = "GigabitEthernet2"  # Biz-Internet Interface

def apply_wan_impairment(ssh, latency_ms=250, loss_percent=3.0):
    """Applies Latency and Packet Loss to simulate a WAN Brownout"""
    print(f"[!] Injecting WAN Impairment: Latency = {latency_ms}ms, Packet Loss = {loss_percent}%...")
    
    # Linux tc netem command structure (if using a Linux WAN emulator node)
    cmd = f"sudo tc qdisc add dev {INTERFACE} root netem delay {latency_ms}ms loss {loss_percent}%"
    
    stdin, stdout, stderr = ssh.exec_command(cmd)
    time.sleep(1)
    print("[+] Impairment applied successfully.")

def clear_wan_impairment(ssh):
    """Clears all injected impairments to restore normal SLA state"""
    print("[!] Clearing WAN Impairments...")
    cmd = f"sudo tc qdisc del dev {INTERFACE} root"
    stdin, stdout, stderr = ssh.exec_command(cmd)
    time.sleep(1)
    print("[+] Link returned to clean state.")

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        ssh.connect(WAN_ROUTER_IP, username=USERNAME, password=PASSWORD)
        print("[+] Connected to WAN Impairment Node.")
        
        # Step 1: Apply Impairment (Failover Trigger)
        apply_wan_impairment(ssh, latency_ms=200, loss_percent=2.5)
        print("[*] Waiting 60 seconds to observe AAR path switch in vManage...")
        time.sleep(60)
        
        # Step 2: Clear Impairment (Recovery Trigger)
        clear_wan_impairment(ssh)
        print("[*] Waiting 60 seconds to observe AAR path failback...")
        time.sleep(60)
        
    except Exception as e:
        print(f"[-] Error: {e}")
    finally:
        ssh.close()

if __name__ == "__main__":
    main()