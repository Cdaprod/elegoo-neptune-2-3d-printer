# Installation

## Requirements

- Docker Desktop or Docker Engine with Compose v2.
- A computer that can reach the printer on the local network.
- MKS WiFi ESP8266 reachable at the configured host and TCP port (normally 8080).
- TCP port 8000 available on the host for the web dashboard.

No cloud account, database server, slicer, or CAD application is required to run the dashboard. OrcaSlicer exports can be analyzed by the G-code tooling, but the app does not control OrcaSlicer profiles.

## Compose install

```sh
git clone https://github.com/Cdaprod/elegoo-neptune-2-3d-printer.git
cd elegoo-neptune-2-3d-printer/integrations/mks-wifi
cp .env.example .env
```

Edit `.env`: set `MKS_WIFI_HOST` to the printer's current network address/name and replace the dashboard token with a unique value. A secure token can be generated with:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put the generated value after `NEPTUNE_OBSERVATORY_TOKEN=`. Keep `.env` private; it is excluded from Git.

Start and check the service:

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 neptune-observatory
```

Open `http://localhost:8000`. On a phone/tablet on the same trusted LAN, browse to the host computer's LAN address at port 8000 and provide the configured token. If it does not load, check the host address, firewall, Wi-Fi isolation, and container health.

## Persistence and updates

SQLite history and command records live in the Compose named volume mounted at `/data`; replacing the container does not remove this volume. `docker compose down` preserves it. Avoid `docker compose down -v` unless you explicitly intend to erase Observatory history. Docker logs rotate at a bounded size. See [storage notes](observatory-storage.md).

Pull updates and rebuild with `docker compose up -d --build`. To stop/resume the app, use `docker compose stop` and `docker compose start`. `restart: unless-stopped` restarts the service after a daemon restart unless you manually stopped it.

## Local development install

For Python 3.12+, install `uv`, enter `integrations/mks-wifi`, and run:

```sh
uv sync --extra dev --extra web --no-editable
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync neptune-observatory --host 127.0.0.1 --port 8000
```

The local server binds to loopback by default. LAN access requires an explicit bind address and dashboard token; see [configuration](configuration.md).
