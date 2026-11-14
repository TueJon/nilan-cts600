#!/usr/bin/env python3
"""Optionally add/remove an explicitly configured compatibility IP alias."""

from __future__ import annotations

import ipaddress
import json
import os
import pathlib
import subprocess
import sys

STATE = pathlib.Path(os.getenv("NILAN_USB_ALIAS_STATE", "/run/nilan-usb-proxy/alias.json"))


def run(*args: str) -> str:
    return subprocess.check_output(["/usr/sbin/ip", *args], text=True).strip()


def main(action: str) -> int:
    value = os.getenv("NILAN_USB_ALIAS", "").strip()
    interface = os.getenv("NILAN_USB_ALIAS_INTERFACE", "").strip()
    if not value:
        return 0
    if action not in {"add", "remove"} or not interface or "/" not in value:
        print("alias configuration requires action, CIDR, and interface", file=sys.stderr)
        return 2
    try:
        address = ipaddress.ip_interface(value)
    except ValueError:
        print("NILAN_USB_ALIAS must be an IP/prefix CIDR", file=sys.stderr)
        return 2
    if address.version != 4 or interface.startswith("-"):
        print("only IPv4 aliases and ordinary interface names are supported", file=sys.stderr)
        return 2

    STATE.parent.mkdir(parents=True, exist_ok=True)
    owned = None
    if STATE.exists():
        try:
            owned = json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            print("alias ownership state is unreadable; refusing cleanup", file=sys.stderr)
            return 1

    rows = run("-o", "-4", "addr", "show").splitlines()
    same_ip_elsewhere = []
    same_ip_here_other_prefix = False
    exact_here = False
    for row in rows:
        fields = row.split()
        if len(fields) < 4 or fields[2] != "inet":
            continue
        existing_ip, _, existing_prefix = fields[3].partition("/")
        if existing_ip == str(address.ip):
            if fields[1] != interface:
                same_ip_elsewhere.append(fields[1])
            elif fields[3] == str(address):
                exact_here = True
            else:
                same_ip_here_other_prefix = True

    if action == "add":
        if owned and (owned.get("address") != str(address) or owned.get("interface") != interface):
            print("existing alias ownership record differs from configured address", file=sys.stderr)
            return 1
        if same_ip_elsewhere:
            print("configured IP already exists on another interface; refusing to move it", file=sys.stderr)
            return 1
        if same_ip_here_other_prefix:
            print("configured IP exists here with another prefix; refusing to alter it", file=sys.stderr)
            return 1
        if exact_here:
            # Pre-existing address belongs to the host/network manager; never
            # claim it, so service stop will not remove somebody else's address.
            return 0
        run("addr", "add", str(address), "dev", interface)
        STATE.write_text(json.dumps({"address": str(address), "interface": interface}) + "\n")
        STATE.chmod(0o600)
        return 0

    # Stop removes only an alias this helper itself added. A manually assigned
    # address or DHCP lease is never considered owned by this service.
    if not owned:
        return 0
    if owned.get("address") != str(address) or owned.get("interface") != interface:
        print("owned alias does not match current configuration; refusing cleanup", file=sys.stderr)
        return 1
    if exact_here:
        run("addr", "del", str(address), "dev", interface)
    STATE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) == 2 else ""))
