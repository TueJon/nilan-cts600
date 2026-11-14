#pragma once
// Copy to secrets.h (ignored), or generate it from protected deployment secrets.
// These defaults seed NVS once. Subsequent Wi-Fi changes persist independently.
#define SEED_WIFI_SSID "replace-me"
#define SEED_WIFI_PASSWORD "replace-with-wifi-password"
#define RECOVERY_AP_PASSWORD "replace-with-unique-strong-password"
#define ADMIN_PASSWORD "replace-with-a-different-strong-password"
// Only this LAN host may open the raw, unencrypted RS485 TCP port.
#define CONTROLLER_IP "192.0.2.10"

