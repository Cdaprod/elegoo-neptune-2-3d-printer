# Neptune Observatory application

This directory contains the FastAPI application, shared MKS WiFi transport,
responsive dashboard, G-code interpreter, Digital Twin, tests, and Docker
Compose deployment. Start at the repository [quick start](../../README.md#quick-start-with-docker-compose).

## Local development

Requirements: Python 3.12 or newer and `uv`.

```sh
uv sync --extra dev --extra web --no-editable
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync neptune-observatory --host 127.0.0.1 --port 8000
```

Set `MKS_WIFI_HOST` and `NEPTUNE_OBSERVATORY_TOKEN` in the process environment
to connect to a printer and use the dashboard. Do not commit `.env` files,
tokens, database files, or real printer network details. Tests use mocked
printer responses and never operate hardware.

The read-only `mks-wifi-probe` utility requires an explicit warning flag because
the Makerbase TCP service accepts one native client at a time and may disconnect
an existing client. Do not run it while another host is using the printer.

## Main modules

- `client.py`, `protocol.py`: bounded TCP client and telemetry parsing.
- `observatory.py`: FastAPI routes, shared poller, storage, and WebSocket.
- `executor.py`, `commands.py`: fixed guarded operations and audit state.
- `motion.py`, `motion_timeline.py`: G-code state and planned motion timelines.
- `mks_upload.py`: separately disabled MKS upload pathway.
- `static/`: application frontend, styles, and vendored Three.js runtime.
- `tests/`: protocol, API, safety, timeline, and frontend logic tests.

See [developer architecture](../../docs/development.md), [MKS protocol
findings](../../docs/mks-wifi-protocol.md), and [Digital Twin notes](docs/digital-twin-and-console.md).
