#!/usr/bin/env python3
"""Generate an ignored build header from an existing protected ESPHome YAML file."""
import argparse
import ipaddress
import json
import os
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--controller-ip", required=True, type=ipaddress.IPv4Address)
    args = parser.parse_args()
    source = yaml.safe_load(args.source.read_text())
    values = {
        "SEED_WIFI_SSID": source["wifi_ssid"],
        "SEED_WIFI_PASSWORD": source["wifi_password"],
        "RECOVERY_AP_PASSWORD": source["ap_password"],
        "ADMIN_PASSWORD": source["ota_password"],
        "CONTROLLER_IP": str(args.controller_ip),
    }
    for key, value in values.items():
        if not isinstance(value, str) or not value or "\0" in value:
            raise SystemExit(f"Invalid value for {key}; no secret values printed")
    if not 1 <= len(values["SEED_WIFI_SSID"].encode()) <= 32:
        raise SystemExit("Wi-Fi SSID must contain 1-32 bytes")
    if not 8 <= len(values["SEED_WIFI_PASSWORD"].encode()) <= 63:
        raise SystemExit("Wi-Fi password must contain 8-63 bytes")
    if any(len(values[k]) < 12 for k in ("RECOVERY_AP_PASSWORD", "ADMIN_PASSWORD")):
        raise SystemExit("Recovery AP and administrator passwords must contain at least 12 characters")
    if len(values["RECOVERY_AP_PASSWORD"].encode()) > 63:
        raise SystemExit("Recovery AP password must contain at most 63 bytes")
    if values["RECOVERY_AP_PASSWORD"] == values["ADMIN_PASSWORD"]:
        raise SystemExit("Use different recovery AP and administrator passwords")
    path = Path(__file__).parent / "include" / "secrets.h"
    content = "#pragma once\n" + "\n".join(
        f"#define {key} {json.dumps(value)}" for key, value in values.items()) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    print("Protected seed header generated; no credentials printed")


if __name__ == "__main__":
    main()
