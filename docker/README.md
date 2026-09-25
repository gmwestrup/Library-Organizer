# Docker / NAS edition

Runs as a container next to your files (on a NAS, or any Linux Docker
host) and serves a browser UI on port `8765`. Copying happens disk-to-
disk on the same machine — no network round trip for a large library.

## Setup

Full guide with NAS path gotchas and a troubleshooting table:
**[`../docs/INSTALL.md`](../docs/INSTALL.md)**.

Quick version:

```bash
# edit the four volume paths first — see docker-compose.yml comments
docker compose up -d
```

Then open `http://<host-ip>:8765`.

## Files in this folder

| File | Purpose |
|---|---|
| `core.py` | the scanning/metadata/copy engine |
| `app.py` | Flask API + web server |
| `templates/index.html` | the entire browser UI |
| `docker-compose.yml` | **default** — no build step, works in Portainer or over SSH |
| `docker-compose.host.yml` | host-networking variant — use if the page is unreachable but `curl localhost:8765` works on the host |
| `docker-compose.build.yml` | builds a self-contained image from `Dockerfile` (SSH only — Portainer stacks have no build context) |
| `portainer-stack.yml` | identical to `docker-compose.yml`, for pasting directly into a Portainer stack |
| `Dockerfile` | used only by the build variant |

## Notes

- No login/auth — this is meant for your LAN only. Don't port-forward it.
- The source volume should be mounted `:ro` (read-only); the app never
  needs write access to your original library.
- See [`../docs/CHANGELOG.md`](../docs/CHANGELOG.md) for what each
  version changed.
