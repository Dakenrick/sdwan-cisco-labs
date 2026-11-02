#!/usr/bin/env python3
"""
DSA-LAB Enterprise Root CA + SD-WAN Controller Certificate Onboarding (TSK-04)
Directory: /home/dakenrick/Documents/EVE-NG/sdwan-cisco-labs/scripts/sdwan_ca.py

Runs on the Ubuntu VM. Requires: pip install cryptography paramiko requests

Subcommands (run in this order the first time):
  init-ca      Create the DSA-LAB root CA (pki/ca/DSA-LAB-ROOT-CA.key/.crt)
  push-root    SSH (VPN 512) to each controller and install the root chain
               (request root-cert-chain install)
  onboard      vManage API: set org-name + vBond, set Enterprise CA + upload root,
               add vSmart/vBond, then for every controller: generate CSR ->
               sign here -> install signed cert
  sign FILE    Offline: sign any CSR file (controller or edge) -> FILE.crt

Examples:
  python3 sdwan_ca.py init-ca
  python3 sdwan_ca.py push-root
  python3 sdwan_ca.py onboard
  python3 sdwan_ca.py sign vsmart.csr
"""

import os
import sys
import time
import argparse
import datetime

import requests
import urllib3
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import lab_plan as plan

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PKI_DIR = os.path.join(BASE_DIR, "pki")
CA_DIR = os.path.join(PKI_DIR, "ca")
CERT_DIR = os.path.join(PKI_DIR, "certs")
CA_NAME = f"{plan.ORG_NAME}-ROOT-CA"
CA_KEY = os.path.join(CA_DIR, f"{CA_NAME}.key")
CA_CRT = os.path.join(CA_DIR, f"{CA_NAME}.crt")
CA_DAYS = 3650
LEAF_DAYS = 1095
CA_KEY_PASS = os.getenv("SDWAN_CA_PASS")      # optional: encrypt the CA key at rest
REMOTE_ROOT_PATH = f"/home/{plan.SDWAN_USER}/{CA_NAME}.crt"


# =============================================================== CA
def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def load_ca():
    if not (os.path.exists(CA_KEY) and os.path.exists(CA_CRT)):
        sys.exit(f"[-] Root CA not found in {CA_DIR}. Run: python3 sdwan_ca.py init-ca")
    with open(CA_KEY, "rb") as f:
        key = serialization.load_pem_private_key(
            f.read(), password=CA_KEY_PASS.encode() if CA_KEY_PASS else None)
    with open(CA_CRT, "rb") as f:
        crt = x509.load_pem_x509_certificate(f.read())
    return key, crt


def init_ca(force=False):
    if os.path.exists(CA_CRT) and not force:
        print(f"[*] Root CA already exists: {CA_CRT} (use --force to replace)")
        return
    os.makedirs(CA_DIR, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, plan.ORG_NAME),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, plan.ORG_NAME),
        x509.NameAttribute(NameOID.COMMON_NAME, f"{plan.ORG_NAME} Root CA"),
    ])
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    crt = (x509.CertificateBuilder()
           .subject_name(name).issuer_name(name)
           .public_key(key.public_key())
           .serial_number(x509.random_serial_number())
           .not_valid_before(_now() - datetime.timedelta(days=1))
           .not_valid_after(_now() + datetime.timedelta(days=CA_DAYS))
           .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
           .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                        key_encipherment=False, data_encipherment=False,
                                        key_agreement=False, key_cert_sign=True, crl_sign=True,
                                        encipher_only=False, decipher_only=False), critical=True)
           .add_extension(ski, critical=False)
           .sign(key, hashes.SHA256()))

    enc = (serialization.BestAvailableEncryption(CA_KEY_PASS.encode()) if CA_KEY_PASS
           else serialization.NoEncryption())
    with open(CA_KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL, enc))
    os.chmod(CA_KEY, 0o600)
    with open(CA_CRT, "wb") as f:
        f.write(crt.public_bytes(serialization.Encoding.PEM))
    print(f"[+] Created root CA '{plan.ORG_NAME} Root CA' (valid {CA_DAYS // 365} years)")
    print(f"    key : {CA_KEY}")
    print(f"    cert: {CA_CRT}")


def sign_csr_pem(csr_pem, days=LEAF_DAYS):
    """Sign a controller/edge CSR with the DSA-LAB root. Returns PEM string."""
    ca_key, ca_crt = load_ca()
    csr = x509.load_pem_x509_csr(csr_pem.encode() if isinstance(csr_pem, str) else csr_pem)
    if not csr.is_signature_valid:
        raise ValueError("CSR signature is invalid")

    ous = [a.value for a in csr.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)]
    if plan.ORG_NAME not in ous:
        print(f"    [!] CSR OU {ous} does not contain org-name '{plan.ORG_NAME}' -- "
              f"check 'organization-name' on the device")

    builder = (x509.CertificateBuilder()
               .subject_name(csr.subject)
               .issuer_name(ca_crt.subject)
               .public_key(csr.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(_now() - datetime.timedelta(days=1))
               .not_valid_after(_now() + datetime.timedelta(days=days))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                            key_encipherment=True, data_encipherment=False,
                                            key_agreement=False, key_cert_sign=False, crl_sign=False,
                                            encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                     ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                              critical=False))
    try:
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        builder = builder.add_extension(san.value, critical=False)
    except x509.ExtensionNotFound:
        pass

    crt = builder.sign(ca_key, hashes.SHA256())
    cn = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    print(f"    [+] Signed: CN={cn[0].value if cn else '?'} OU={','.join(ous)} "
          f"(valid {days // 365} years)")
    return crt.public_bytes(serialization.Encoding.PEM).decode()


def sign_file(path, days=LEAF_DAYS):
    with open(path) as f:
        pem = sign_csr_pem(f.read(), days)
    out = os.path.splitext(path)[0] + ".crt"
    with open(out, "w") as f:
        f.write(pem)
    print(f"[+] Wrote {out}")


# =============================================================== push root chain (SSH)
def _read_until(chan, patterns, timeout=30):
    buf, end = "", time.time() + timeout
    while time.time() < end:
        if chan.recv_ready():
            buf += chan.recv(65535).decode(errors="ignore")
            if any(p in buf for p in patterns):
                return buf
        else:
            time.sleep(0.2)
    return buf


def push_root(only=None):
    import paramiko

    _, _ = load_ca()
    with open(CA_CRT) as f:
        root_pem = f.read().strip()

    for host, c in plan.CONTROLLERS.items():
        if only and host not in only:
            continue
        ip = c["mgmt_ip"]
        print(f"[*] {host} ({ip}): installing root chain")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(ip, username=plan.SDWAN_USER, password=plan.SDWAN_PASS,
                        look_for_keys=False, allow_agent=False, timeout=15)
            chan = ssh.invoke_shell(width=250)
            _read_until(chan, ["#"], 20)

            # Write the root cert from the Linux shell, then install from the CLI
            chan.send("vshell\n")
            _read_until(chan, ["$"], 10)
            chan.send(f"cat > {REMOTE_ROOT_PATH} << 'EOF_ROOT'\n{root_pem}\nEOF_ROOT\n")
            _read_until(chan, ["$"], 10)
            chan.send("exit\n")
            _read_until(chan, ["#"], 10)

            chan.send(f"request root-cert-chain install {REMOTE_ROOT_PATH}\n")
            out = _read_until(chan, ["Successfully", "rror", "ailed"], 60)
            ok = "Successfully" in out
            print(f"    [{'+' if ok else '-'}] {out.strip().splitlines()[-1] if out.strip() else 'no output'}")
        except Exception as err:  # noqa: BLE001 - report and keep going
            print(f"    [-] {host}: {err}")
        finally:
            ssh.close()


# =============================================================== vManage API onboarding
class VManage:
    def __init__(self, host, user, password):
        self.base = f"https://{host}"
        self.s = requests.Session()
        self.s.verify = False
        r = self.s.post(f"{self.base}/j_security_check",
                        data={"j_username": user, "j_password": password}, timeout=30)
        if r.status_code != 200 or "<html" in r.text.lower():
            sys.exit(f"[-] vManage login failed at {host} (check credentials / that vManage is fully up)")
        tok = self.s.get(f"{self.base}/dataservice/client/token", timeout=30)
        if tok.status_code == 200 and tok.text:
            self.s.headers["X-XSRF-TOKEN"] = tok.text
        self.s.headers["Content-Type"] = "application/json"
        print(f"[+] Logged in to vManage {host}")

    def call(self, method, path, payload=None, label=""):
        r = self.s.request(method, f"{self.base}/dataservice{path}", json=payload, timeout=120)
        ok = r.status_code in (200, 201, 204)
        if not ok:
            print(f"    [-] {label or path}: HTTP {r.status_code} {r.text[:300]}")
        try:
            return ok, r.json() if r.text else {}
        except ValueError:
            return ok, {"raw": r.text}

    def put_or_post(self, path, payload, label):
        ok, body = self.call("PUT", path, payload, label + " (PUT)")
        if not ok:
            ok, body = self.call("POST", path, payload, label + " (POST)")
        if ok:
            print(f"    [+] {label}")
        return ok, body


def onboard():
    load_ca()
    with open(CA_CRT) as f:
        root_pem = f.read()

    vm = VManage(plan.VMANAGE_MGMT_IP, plan.SDWAN_USER, plan.SDWAN_PASS)

    print("\n--- vManage settings ---")
    vm.put_or_post("/settings/configuration/organization",
                   {"org": plan.ORG_NAME}, f"Organization name = {plan.ORG_NAME}")
    vm.put_or_post("/settings/configuration/device",
                   {"domainIp": plan.VBOND_PRIVATE_IP, "port": str(plan.VBOND_PORT)},
                   f"vBond = {plan.VBOND_PRIVATE_IP}:{plan.VBOND_PORT}")
    vm.put_or_post("/settings/configuration/certificate",
                   {"certificateSigning": "enterprise"}, "Controller certificate authorization = Enterprise")
    vm.put_or_post("/settings/configuration/certificate/enterpriserootca",
                   {"enterpriseRootCA": root_pem}, "Enterprise root CA uploaded")

    print("\n--- Adding controllers ---")
    ok, body = vm.call("GET", "/system/device/controllers", label="list controllers")
    existing = {d.get("deviceIP") for d in body.get("data", [])} if ok else set()
    for host, c in plan.CONTROLLERS.items():
        if c["personality"] == "vmanage":
            continue
        if c["vpn0_ip"] in existing:
            print(f"    [*] {host} already added")
            continue
        ok, _ = vm.call("POST", "/system/device", {
            "deviceIP": c["vpn0_ip"], "username": plan.SDWAN_USER, "password": plan.SDWAN_PASS,
            "personality": c["personality"], "generateCSR": False, "protocol": "DTLS", "port": "",
        }, label=f"add {host}")
        if ok:
            print(f"    [+] Added {host} ({c['personality']}) at {c['vpn0_ip']}")
        time.sleep(5)

    print("\n--- Certificates: generate CSR -> sign -> install ---")
    ok, body = vm.call("GET", "/system/device/controllers", label="list controllers")
    controllers = body.get("data", []) if ok else []
    if not controllers:
        sys.exit("[-] No controllers returned by vManage; nothing to certify.")

    os.makedirs(CERT_DIR, exist_ok=True)
    for dev in controllers:
        dev_ip = dev.get("deviceIP")
        name = dev.get("host-name") or dev.get("deviceType") or dev_ip
        if dev.get("certInstallStatus", "").lower() == "installed":
            print(f"    [*] {name}: certificate already installed")
            continue
        print(f"[*] {name} ({dev_ip})")
        ok, body = vm.call("POST", "/certificate/generate/csr", {"deviceIP": dev_ip}, label=f"{name} CSR")
        csr_pem = None
        if ok:
            data = body.get("data")
            if isinstance(data, list) and data:
                csr_pem = data[0].get("deviceCSR")
            elif isinstance(data, dict):
                csr_pem = data.get("deviceCSR")
        if not csr_pem:
            print(f"    [-] {name}: no CSR returned ({str(body)[:200]})")
            continue

        safe = name.replace("/", "_")
        with open(os.path.join(CERT_DIR, f"{safe}.csr"), "w") as f:
            f.write(csr_pem)
        crt_pem = sign_csr_pem(csr_pem)
        with open(os.path.join(CERT_DIR, f"{safe}.crt"), "w") as f:
            f.write(crt_pem)

        ok, _ = vm.call("POST", "/certificate/install/signedCert", {"cert": crt_pem},
                        label=f"{name} install cert")
        if ok:
            print(f"    [+] {name}: signed certificate installed")
        time.sleep(5)

    print("\n[*] Done. In vManage: Configuration > Certificates > Controllers should show all three Installed.")
    print("[*] Then check: show control connections (vSmart/vBond) and 'show orchestrator connections' (vBond).")


# =============================================================== CLI
def main():
    p = argparse.ArgumentParser(description=f"{plan.ORG_NAME} enterprise CA + SD-WAN controller onboarding")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init-ca", help="create the root CA")
    s.add_argument("--force", action="store_true", help="replace an existing root CA")
    s = sub.add_parser("push-root", help="install root chain on controllers via SSH")
    s.add_argument("--only", nargs="*", help="limit to these controller names")
    sub.add_parser("onboard", help="vManage API: settings, add controllers, sign + install certs")
    s = sub.add_parser("sign", help="sign a CSR file offline")
    s.add_argument("csr")
    s.add_argument("--days", type=int, default=LEAF_DAYS)
    args = p.parse_args()

    if args.cmd == "init-ca":
        init_ca(args.force)
    elif args.cmd == "push-root":
        push_root(args.only)
    elif args.cmd == "onboard":
        onboard()
    elif args.cmd == "sign":
        sign_file(args.csr, args.days)


if __name__ == "__main__":
    main()
