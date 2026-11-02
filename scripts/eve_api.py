#!/usr/bin/env python3
"""
Shared EVE-NG Pro API helpers (login, lock-retry, Pro JSON parsing, config inject)
Directory: /home/dakenrick/Documents/EVE-NG/sdwan-cisco-labs/scripts/eve_api.py
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
CONFIG_SET = os.getenv("EVE_CFSID", "default")

BASE_URL = f"https://{EVE_IP}/api"
SESSION = requests.Session()
SESSION.verify = False
SESSION.headers.update({"Content-Type": "application/json"})

LOCK_RETRIES = 5
LOCK_BACKOFF = 2.0
PACE = 0.5


class EveCrash(Exception):
    """EVE returned an HTML error page instead of JSON (PHP error/500)."""


def short(text, limit=300):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:limit]


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
        print(f"[-] EVE login failed: {short(res.text)}")
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
    """Push a startup config (EVE Pro format) and flag the node to boot from it."""
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


def inject_many(configs):
    """configs: {node_name: config_text}. Returns {node_name: bool}."""
    eve_login()
    results = {}
    try:
        name_to_id = {info["name"]: nid for nid, info in get_nodes().items()}
        for host, text in configs.items():
            node_id = name_to_id.get(host)
            if not node_id:
                print(f"[!] Node '{host}' not found in lab. Skipping.")
                results[host] = False
                continue
            results[host] = inject_config(node_id, host, text)
    except EveCrash as err:
        print(f"\n[-] EVE returned an error page, stopping:\n    {err}")
        sys.exit(2)
    finally:
        eve_logout()
    return results
