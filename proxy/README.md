# nx-neptune-proxy

A local FastAPI server with React UI for managing Neptune Analytics graph projections from data lake sources.

## Quick Start

```bash
# Install dependencies
pip install -e ".[dev]"

# Run the server
make dev

# Or with Docker
make up
```

The UI is available at `http://localhost:8080`.

## Architecture

- **Backend**: FastAPI (Python) — manages projections, executes Athena queries, orchestrates Neptune graph lifecycle
- **Frontend**: React + TypeScript + Vite — SPA served by FastAPI in production
- **Storage**: SQLite (`~/.nx-neptune/proxy.db`) — stores project and projection metadata
- **Graph Explorer**: Optional sidecar container for visual graph exploration

## Docker Compose

The `docker-compose.yml` runs both the proxy and Graph Explorer locally:

```bash
export AWS_DEFAULT_REGION=<your region>   # required: make up refuses to start without it
make ui up RUNTIME=docker                 # or podman / finch (the default)
```

- The proxy UI is at `http://127.0.0.1:8080`, Graph Explorer at
  `https://localhost:8443/explorer` (self-signed certificate). Both listen on the
  loopback interface only. Set `GRAPH_EXPLORER_PORT` to use another port for Graph
  Explorer; build the UI (`make ui`) with the same value so its "Open in Graph
  Explorer" links match.
- Projects and projections are kept in the `proxy-home` named volume and survive
  `make up` and container recreation. `docker compose down -v` deletes them.
- `NETWORKX_ATHENA_WORKGROUP` (optional) selects the Athena workgroup for projection
  queries; unset uses the account's primary workgroup.
- `GRAPH_EXPLORER_IMAGE` (optional) pins the Graph Explorer image, for example by
  digest. The image is published for `linux/amd64` only; on Apple Silicon it runs
  under emulation.
- `make up` fails when compose fails.

### ⚠️ Security: AWS Credentials

AWS credentials are passed via environment variables for **local development only**. Never use long-lived credentials (IAM user access keys). Always use short-lived session tokens from:

- `aws sso login`
- `aws sts assume-role`
- Instance profile (when running on EC2/ECS)

Do not deploy this docker-compose configuration to shared or production environments.

## Development

```bash
# Run backend tests
pytest tests/ -q

# Build UI
cd ui-src && npm run build

# Run UI dev server (hot reload)
cd ui-src && npm run dev
```

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | Server port |
| `CORS_ALLOWED_ORIGINS` | *(none)* | Comma-separated list of allowed browser origins (full `scheme://host[:port]`, e.g. `https://app.example.com`). This is the single source of truth for origins: it drives the CORS response headers, the server-side Origin check, and the hostnames trusted in the `Host` header. Loopback origins are always allowed, so no configuration is needed for local/Docker use. |
| `LOG_LEVEL` | `info` | Logging level |
| `NX_NEPTUNE_DB_PATH` | `~/.nx-neptune/proxy.db` | SQLite database path |
