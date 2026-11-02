#!/usr/bin/env python3
"""
SD-WAN Controller First-Boot Console Bootstrap (vManage persona/disk + Day-0 config)
Directory: /home/dakenrick/Documents/EVE-NG/sdwan-cisco-labs/scripts/controller_console_bootstrap.py

Drives each controller's EVE-NG telnet console and answers the interactive first-boot prompts:
  - login admin/admin  -> forced initial password change (to $SDWAN_PASS)
  - vManage only: "Select persona" -> 1 (COMPUTE_AND_DATA), confirm -> y,
                  "Select storage device" -> 1, "format?" -> y  (builds the DBs on disk 2, reboots)
  - after the final login: if the Day-0 config isn't present (org-name missing),
    pastes configs/sdwan/<node>.cfg in config mode and commits it.

Usage (start the controller nodes in EVE first; run while they boot):
  export SDWAN_PASS='YourNewAdminPass'
  python3 controller_console_bootstrap.py                    # all three, vManage first
  python3 controller_console_bootstrap.py --only vManage-01
  python3 controller_console_bootstrap.py --only vSmart-01 --port 32771   # manual console port
"""

import os
import re
import sys
import time
import socket
import argparse

import lab_plan as plan
import eve_api

CFG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs", "sdwan")
DEFAULT_PASS = "admin"
BOOT_TIMEOUT = 60 * 60          # vManage first boot + DB build can be long
CMD_TIMEOUT = 60

# telnet protocol bytes
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA = 1, 3


class Console:
    """Minimal telnet client (telnetlib was removed in Python 3.13)."""

    def __init__(self, host, port, log_name):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(0.5)
        self.buf = ""
        self.log_name = log_name

    def _negotiate(self, data):
        out, i, reply = bytearray(), 0, bytearray()
        while i < len(data):
            b = data[i]
            if b == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (DO, DONT, WILL, WONT) and i + 2 < len(data):
                    opt = data[i + 2]
                    if cmd == DO:
                        reply += bytes([IAC, WONT, opt])
                    elif cmd == WILL:
                        reply += bytes([IAC, DO if opt in (ECHO, SGA) else DONT, opt])
                    i += 3
                    continue
                if cmd == SB:
                    end = data.find(bytes([IAC, SE]), i)
                    i = len(data) if end < 0 else end + 2
                    continue
                if cmd == IAC:
                    out.append(IAC)
                i += 2
                continue
            out.append(b)
            i += 1
        if reply:
            self.sock.sendall(bytes(reply))
        return out.decode(errors="ignore")

    def read(self):
        try:
            data = self.sock.recv(65535)
        except socket.timeout:
            return ""
        if not data:
            raise ConnectionError("console closed")
        text = self._negotiate(data)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
            self.buf = (self.buf + text)[-4000:]
        return text

    def send(self, line, delay=0.3):
        self.sock.sendall((line + "\r").encode())
        time.sleep(delay)

    def wait_for(self, patterns, timeout=CMD_TIMEOUT):
        end = time.time() + timeout
        while time.time() < end:
            self.read()
            for p in patterns:
                if re.search(p, self.buf, re.I | re.M):
                    return p
        return None

    def run(self, cmd, timeout=CMD_TIMEOUT):
        self.buf = ""
        self.send(cmd)
        self.wait_for([r"[\w\-.]+(\(config[^)]*\))?#\s*$"], timeout)
        return self.buf

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


PROMPT_RE = r"^[\w\-.]+#\s*$"


def pick_storage(text):
    """Choose the largest real disk from vManage's storage menu, never a CD-ROM (sr*)
    such as EVE's config ISO. Menu looks like:
        vdb     100GB
        sr0     0GB
        1) vdb
        2) sr0
    """
    sizes = {m.group(1): float(m.group(2))
             for m in re.finditer(r"^\s*(\w+)\s+([\d.]+)\s*[GT]B", text, re.M)}
    options = [(m.group(1), m.group(2)) for m in re.finditer(r"^\s*(\d+)\)\s*(\w+)", text, re.M)]
    disks = [(num, dev) for num, dev in options if not dev.startswith(("sr", "cdrom", "fd"))]
    if not disks:
        return "1", "unknown"
    num, dev = max(disks, key=lambda nd: sizes.get(nd[1], 0))
    return num, dev


def first_boot(con, node, personality, new_pass):
    """Answer login/password/persona/disk prompts until a CLI prompt is reached."""
    candidates = [DEFAULT_PASS, new_pass]
    pw_idx = 0
    expecting_new_pw = False
    sent_pw = False          # did WE send a login password since the last prompt?
    failures = 0             # Viptela locks the account after 5 failures; stop at 3
    max_failures = 3
    initializing = False     # vManage rejects logins while "System Initializing"
    # Viptela uses both "(y/n)" and "[y/n]" -- match either
    yes_no = r"[\(\[]\s*y\s*/\s*n\s*[\)\]]\s*[:?]?\s*$"
    # A blank Enter only to make a silent console reprint its prompt (initial attach).
    con.send("")
    end = time.time() + BOOT_TIMEOUT
    last_output = time.time()

    while time.time() < end:
        if con.read():
            last_output = time.time()
        tail = con.buf[-400:]
        low = tail.lower()

        if "system initializing" in low or "please wait to login" in low:
            initializing = True
        if "system ready" in low:
            initializing = False

        if re.search(PROMPT_RE, tail, re.M):
            print(f"\n[+] {node}: logged in")
            return True
        if "account is locked" in low:
            m = re.search(r"\((\d+) minutes? left", low)
            print(f"\n[-] {node}: admin account is locked"
                  f"{f' ({m.group(1)} min left)' if m else ''}. Wait for it to unlock, or Stop + Wipe + "
                  f"Start the node, then rerun with --only {node}.")
            return False
        if re.search(r"(format|erase).*" + yes_no, low, re.S):
            con.buf = ""; con.send("y")
            print(f"\n[*] {node}: formatting data disk; it will reboot and build its databases "
                  f"(this can take 15-30+ min)...")
        elif re.search(yes_no, low):
            # persona "Are you sure? [y/n]" and any other confirmation
            con.buf = ""; con.send("y"); print(f"\n[*] {node}: confirmed (y)")
        elif re.search(r"select persona.*:\s*$", low, re.S):
            con.buf = ""; con.send("1"); print(f"\n[*] {node}: persona COMPUTE_AND_DATA")
        elif re.search(r"select storage device.*:\s*$", low, re.S):
            choice, dev = pick_storage(tail)
            con.buf = ""; con.send(choice)
            print(f"\n[*] {node}: storage device {choice} ({dev}) selected")
        elif "login incorrect" in low:
            # keep anything after the message (often the next "login:" prompt arrives with it)
            con.buf = tail[low.rfind("login incorrect") + len("login incorrect"):]
            # Only count failures caused by a password we sent (not our attach Enter,
            # and not rejections while the system is still initializing).
            if sent_pw and not initializing:
                failures += 1
                pw_idx = (pw_idx + 1) % len(candidates)      # alternate admin <-> $SDWAN_PASS
                if failures >= max_failures:
                    print(f"\n[-] {node}: {failures} failed logins with both 'admin' and $SDWAN_PASS; "
                          f"stopping before the account locks. Check the node's admin password.")
                    return False
            sent_pw = False
            expecting_new_pw = False
        elif re.search(r"re-?enter( new)? password:\s*$", low):
            con.buf = ""; con.send(new_pass, 1)
        elif re.search(r"(initial admin password|new password|change.*password)", low) and \
                re.search(r"password:\s*$", low):
            con.buf = ""; con.send(new_pass, 1); expecting_new_pw = True; pw_idx = 1
            print(f"\n[*] {node}: setting initial admin password")
        elif re.search(r"password:\s*$", low):
            con.buf = ""
            if expecting_new_pw:
                con.send(new_pass, 1)
            else:
                con.send(candidates[pw_idx], 1)
                sent_pw = True
        elif re.search(r"login:\s*$", low):
            con.buf = ""; con.send(plan.SDWAN_USER)
        elif time.time() - last_output > 120 and not re.search(r"[:\]\)?]\s*$", tail):
            # Console silent for 2 min and not sitting at a question: nudge it to reprint
            # the prompt. Never sent at a y/n or selection prompt (blank = "no" there).
            con.send("")
            last_output = time.time()
    print(f"\n[-] {node}: timed out waiting for a CLI prompt")
    return False


def render_configs():
    """Always re-render from lab_plan.py so the pasted config is never stale."""
    import build_sdwan_controller_configs as builder
    os.makedirs(CFG_DIR, exist_ok=True)
    for host, text in builder.render_all().items():
        with open(os.path.join(CFG_DIR, f"{host}.cfg"), "w") as f:
            f.write(text)
    print(f"[*] Rendered Day-0 configs to {CFG_DIR}")


def apply_day0(con, node, force=False):
    path = os.path.join(CFG_DIR, f"{node}.cfg")

    con.run("paginate false")
    out = con.run("show running-config system organization-name")
    if plan.ORG_NAME in out and not force:
        print(f"\n[*] {node}: Day-0 config already present (org {plan.ORG_NAME}), skipping paste "
              f"(use --force-config to re-apply)")
        return True

    print(f"\n[*] {node}: pasting {os.path.basename(path)}")
    con.run("config")
    with open(path) as f:
        for line in f:
            if line.strip():
                con.send(line.rstrip("\n"), 0.15)
                con.read()
    out = con.run("commit and-quit", 120)
    ok = "commit complete" in out.lower() or "no modifications" in out.lower()
    if not ok:
        con.run("abort")      # leave config mode and discard the failed candidate config
    print(f"\n[{'+' if ok else '-'}] {node}: commit {'complete' if ok else 'FAILED - check console output'}")
    return ok


PORT_WAIT = 600     # seconds to wait for the node to start and get a console port


def _console_port(info):
    """EVE gives the console in 'url' (telnet://host:port); Pro returns port 0 until the node runs."""
    url = str(info.get("url", ""))
    m = re.search(r"telnet://([^:/]+):(\d+)", url)
    if m and int(m.group(2)) > 0:
        return m.group(1), int(m.group(2))
    for key in ("console_port", "port", "telnet_port"):
        try:
            port = int(info.get(key) or 0)
        except (TypeError, ValueError):
            port = 0
        if port > 0:
            return eve_api.EVE_IP, port
    return None


def console_target(node, port_override=None):
    if port_override:
        return eve_api.EVE_IP, port_override

    eve_api.eve_login()
    try:
        end = time.time() + PORT_WAIT
        warned = False
        info = {}
        while time.time() < end:
            info = next((i for i in eve_api.get_nodes().values() if i.get("name") == node), None)
            if info is None:
                print(f"[-] {node}: not found in lab")
                return None
            target = _console_port(info)
            if target:
                return target
            if not warned:
                print(f"[*] {node}: no console port yet (status={info.get('status')}, url='{info.get('url')}'). "
                      f"Start the node in EVE -- waiting up to {PORT_WAIT // 60} min...")
                warned = True
            time.sleep(10)
        print(f"[-] {node}: still no console port. EVE node data: "
              f"{ {k: info.get(k) for k in ('status', 'url', 'console', 'console_port', 'port')} }")
        print(f"    Open the node's console in EVE, note the telnet port, and rerun with "
              f"--only {node} --port <port>")
        return None
    finally:
        eve_api.eve_logout()


def main():
    p = argparse.ArgumentParser(description="Answer SD-WAN controller first-boot prompts over the EVE console")
    p.add_argument("--only", nargs="*", help="controller names (default: all, vManage first)")
    p.add_argument("--port", type=int, help="telnet console port (only with a single --only node)")
    p.add_argument("--skip-config", action="store_true", help="don't paste the Day-0 config")
    p.add_argument("--force-config", action="store_true",
                   help="paste the Day-0 config even if org-name is already set (re-apply fixes)")
    args = p.parse_args()

    new_pass = plan.SDWAN_PASS
    if not new_pass or new_pass == DEFAULT_PASS:
        sys.exit("[-] Set the new admin password first:  export SDWAN_PASS='YourNewAdminPass'  "
                 "(controllers won't accept 'admin' as the new password)")

    order = ["vManage-01", "vSmart-01", "vBond-01"]
    nodes = [n for n in order if not args.only or n in args.only]
    if args.port and len(nodes) != 1:
        sys.exit("[-] --port needs exactly one node in --only")

    render_configs()
    for node in nodes:
        print("=" * 65 + f"\n   {node}\n" + "=" * 65)
        target = console_target(node, args.port)
        if not target:
            continue
        host, port = target
        print(f"[*] Console {host}:{port}  (Ctrl+C to abort)")
        try:
            con = Console(host, port, node)
        except OSError as err:
            print(f"[-] {node}: can't open console {host}:{port} ({err}). Is the node running and the "
                  f"console set to telnet (not HTML5)?")
            continue
        try:
            if first_boot(con, node, plan.CONTROLLERS[node]["personality"], new_pass) and not args.skip_config:
                apply_day0(con, node, args.force_config)
        except (ConnectionError, OSError) as err:
            print(f"\n[-] {node}: console error: {err}")
        finally:
            con.close()


if __name__ == "__main__":
    main()
