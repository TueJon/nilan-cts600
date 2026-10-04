# Nilan CTS600 — Server Stack (TUE-22)

Server-side "brain" for the Nilan CTS600 WLAN control (parent: TUE-16). The
ESP32 is only a transparent **raw serial↔TCP tunnel**; the CTS600 custom
protocol is driven here by **frodef**'s `CTS600` class over a serial device,
and exposed as the REST + MQTT contract from TUE-16 plan §8.

```
Nilan ── RS485 ──> ESP32 (raw TCP :6638) ──WLAN──> [ socat ─> /dev/ttyNILAN ─> frodef ] ─> REST + MQTT
```

## Architecture decision

Chose **standalone frodef core + thin FastAPI/MQTT wrapper** (plan §7 sanctioned
alternative) over full Home Assistant:

- The §8 contract (`/api/*` + `nilan/...` topics) is custom; HA would still need
  a shim to remap its native API/MQTT-discovery topics to it. The wrapper
  implements §8 directly with less total glue.
- Far lighter for a deployment host with several other services already
  resident. Whole stack is capped well under 0.5 GB vs HA's ~1–2 GB.
- `frodef`'s protocol library (`vendor/nilan_cts600.py`, pinned commit in
  `vendor/FRODEF_COMMIT.txt`) imports cleanly standalone, incl. `CTS600Mockup`.

Reversible: if the board prefers full HA, frodef is its native HACS integration;
the socat/broker/proxy layers here are reused as-is.

## Layout

| Path | Purpose |
|---|---|
| `vendor/nilan_cts600.py` | Pinned frodef CTS600 protocol lib (incl. mockup) |
| `app/nilan_api.py` | FastAPI REST + MQTT wrapper (implements plan §8) |
| `app/Dockerfile`, `app/entrypoint.sh` | API image; entrypoint runs socat sidecar in real mode |
| `docker-compose.yml` | `mosquitto` + `nilan-api`; retired Caddy fallback is profile-gated |
| `mosquitto/` | broker config + `passwd` (gitignored) |
| `caddy/Caddyfile` | reverse proxy + basic auth on `:8643` |
| `env/nilan.env(.example)` | config + secrets (`.env` gitignored) |
| `systemd/socat-nilan.service` | OPTIONAL host-side socat (only for a host consumer; container has its own) |

## Operator Documentation

- [`docs/architecture-setup-debugging.md`](docs/architecture-setup-debugging.md)
  is the canonical restart point for architecture, host split, live addresses,
  setup commands, safety gates, API/MQTT surfaces, physical bring-up, rollback,
  debugging, and activity-log ownership.
- [`docs/nilan-install-handoff.md`](docs/nilan-install-handoff.md) is the
  on-site wiring and agent handoff guide for first physical bring-up.

## Bring-up NOW (no hardware — mockup)

```bash
cd <repo-dir>
cp env/nilan.env.example env/nilan.env      # NILAN_MOCKUP=1 by default
# secrets:
PW=$(openssl rand -hex 12)
docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 mosquitto_passwd -b -c /m/passwd nilan "$PW"
# mosquitto_passwd writes the file 0600 root; the in-container mosquitto user must read it:
docker run --rm -v "$PWD/mosquitto:/m" --entrypoint sh eclipse-mosquitto:2 -c 'chmod 0644 /m/passwd'
sed -i "s/^MQTT_PASS=.*/MQTT_PASS=$PW/" env/nilan.env
docker compose up -d --build mosquitto nilan-api
docker compose exec -T nilan-api curl -s http://127.0.0.1:8642/api/status | jq
```

## Go live (ESP bridge installed)

When Wattson reports the live/static ESP IP on TUE-16:

```bash
cd <repo-dir>
sed -i 's/^NILAN_MOCKUP=.*/NILAN_MOCKUP=0/' env/nilan.env
sed -i 's/^ESP_IP=.*/ESP_IP=<bridge-ip>/'  env/nilan.env
docker compose up -d mosquitto nilan-api
docker compose logs -f nilan-api   # expect socat tunnel + frodef reads T15/display
docker compose exec -T nilan-api curl -s http://127.0.0.1:8642/api/status | jq
```

Then run the §9 control verification (fan 1→2→3, mode, setpoint set+readback) and
confirm the provisional `NILAN_T_SUPPLY_KEY`/`NILAN_T_EXHAUST_KEY` mapping against
the unit's register dump (TUE-16 §11).

## Dashboard and legacy Caddy rollback

HomeBoard is the supported dashboard and control surface. The old Nilan Caddy
listener on TCP/8643 is retired: it is excluded from default Compose operations,
binds loopback only even when explicitly enabled, and must not be published through
Tailscale Funnel, Cloudflare Tunnel, or a host firewall rule.

The API and its bundled historical dashboard remain available inside the Compose
network for diagnostics:

```bash
docker compose exec -T nilan-api curl -s http://127.0.0.1:8642/api/status | jq
```

Emergency local-only rollback:

```bash
docker compose --profile legacy-ui up -d caddy
curl -u nilan:'<password-from-local-secret-store>' http://127.0.0.1:8643/api/status
docker compose --profile legacy-ui stop caddy
```

Do not remove the `legacy-ui` profile or the `127.0.0.1` bind when testing rollback.

## Contract (plan §8)

| Method | Endpoint / topic | Body / payload |
|---|---|---|
| GET | `/api/status` | `{t_room,t_supply,t_exhaust,fan_level,mode,setpoint,...}` |
| GET | `/api/activity?include_reads=false&limit=100` | Bounded activity log; set `include_reads=true` to include summarized polling |
| POST | `/api/fan` | `{"level":0-4}` (0=off) |
| POST | `/api/mode` | `{"mode":"auto\|heat\|cool\|off"}` |
| POST | `/api/temp` | `{"setpoint":5-30}` |
| POST | `/api/room` | `{"celsius":5.0-35.0,"source":"<name>"}` — live room temperature (see below) |
| MQTT sub | `nilan/fan/set`,`nilan/mode/set`,`nilan/temp/set` | as above (raw value or JSON) |
| MQTT sub | `nilan/room/set` | plain number in °C (source is recorded as `mqtt`) |
| MQTT pub | `nilan/state` (retained) | JSON, same shape as `/api/status` |
| MQTT pub | `nilan/availability` (retained, LWT) | `online`/`offline` |

## Live room temperature

The panel's room sensor (T15) is gone, so the daemon injects the value the unit
regulates on. Without a feed it injects the constant `NILAN_T15_FALLBACK`. A
separate sensor can supply a real value instead:

- `POST /api/room` with `{"celsius": 22.4, "source": "living-room"}`, or
- publish `22.4` to `nilan/room/set`.

Values outside 5.0-35.0 are rejected (`422`; MQTT: ignored). Both paths respect
`NILAN_READ_ONLY` (blocked and logged, like every other command) and are written
to the activity log. The value is rounded to the unit's resolution (about
0.14 °C) and sent under the same device lock as all other commands.

A live value is only trusted for `NILAN_ROOM_TTL_SECONDS` after it was received.
Keep sending it more often than that (once a minute is plenty). When it expires
the daemon falls back to `NILAN_T15_FALLBACK` and logs it once. After a
reconnect to the unit a live value that is still within its TTL is re-applied.

`GET /api/status` (and the retained `nilan/state`) reports both modes:

```json
"room_source": {"mode": "live", "value": 22.4, "fallback": 21.0,
                "age_s": 12.3, "source": "living-room", "ttl_s": 900.0}
```

`mode` is `fallback` (with `age_s` and `source` null) when no fresh live value
exists. `t_room` keeps showing the temperature the unit actually uses.

| Env var | Default | Meaning |
|---|---|---|
| `NILAN_T15_FALLBACK` | `21` | Room temperature used when no fresh live value exists |
| `NILAN_ROOM_TTL_SECONDS` | `900` | Max age of a live value; `0` disables live values entirely |

Rollback: stop sending values and the unit returns to the fallback after the
TTL. To turn the feature off immediately, set `NILAN_ROOM_TTL_SECONDS=0` and
restart the API: live values are then refused (`409`; MQTT: ignored) and the
fallback is always used.

## Tests

```bash
pip install -r app/requirements.txt pytest httpx
python -m pytest tests
```

The tests run the app in mockup mode; no hardware or broker is needed.
