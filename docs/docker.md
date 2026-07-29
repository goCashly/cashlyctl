# Docker

Status: initial implementation  
Scope: local `cashlyctl` image packaging

## Build

From `cashlyctl-src`:

```bash
docker build -t cashlyctl:local .
```

## Run

The image uses `cashlyctl` as its entrypoint:

```bash
docker run --rm cashlyctl:local --help
docker run --rm cashlyctl:local profile list
docker run --rm cashlyctl:local health
docker run --rm cashlyctl:local system inspect-host
```

Use a volume for persistent `~/.cashlyctl` state:

```bash
docker volume create cashlyctl-state
docker run --rm -it \
  -v cashlyctl-state:/home/cashly/.cashlyctl \
  cashlyctl:local console
```

On first run without dev credentials, type `INITADMIN` in the console to create the local admin user. The user record is stored in the mounted state volume as a salted password hash.

Load runtime configuration from an env file when needed:

```bash
docker run --rm --env-file .env \
  -v cashlyctl-state:/home/cashly/.cashlyctl \
  cashlyctl:local health --profile enterprise-stage
```

Or mount the env file where `cashlyctl` can read it directly:

```bash
docker run --rm -it \
  -v cashlyctl-state:/home/cashly/.cashlyctl \
  -v "$PWD/.env:/home/cashly/.cashlyctl/.env:ro" \
  cashlyctl:local console
```

For local-only convenience, copy `.env` into the persistent state volume once:

```bash
docker run --rm \
  -v cashlyctl-state:/home/cashly/.cashlyctl \
  -v "$PWD/.env:/tmp/cashlyctl.env:ro" \
  --entrypoint /bin/sh \
  cashlyctl:local \
  -c 'cp /tmp/cashlyctl.env /home/cashly/.cashlyctl/.env'

docker run --rm -it \
  -v cashlyctl-state:/home/cashly/.cashlyctl \
  cashlyctl:local console
```

This gives repeat runs packaged-like behavior without baking secrets into image layers.

The Docker build context intentionally ignores `.env`, so real local secrets are not copied into image layers by accident. If Docker Desktop rejects a bind mount path, move the env file under the project/home directory or add the path in Docker Desktop file-sharing settings.

Runtime env lookup order:

1. Process environment variables.
2. `CASHLYCTL_ENV_FILE`, when set.
3. `$CASHLYCTL_HOME/.env`.
4. `/app/.env`.
5. `.env` in the current working directory.

## Networking Notes

Inside Docker, `127.0.0.1` means the container, not the host. For local host services:

- On Docker Desktop, use `host.docker.internal` in profile URLs.
- On Linux, use `--network host` when appropriate, or configure reachable host/container network addresses.

## Arch Linux / Native Linux Notes

If `cashlyctl system inspect-host` reports `Docker CLI available` but `Docker socket missing`, Docker is installed but the current shell cannot see a running daemon/socket. Common Arch Linux checks:

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

After changing group membership, log out and back in, or start a fresh login shell. For rootless Docker, ensure `DOCKER_HOST` points at the rootless socket, such as `unix:///run/user/$UID/docker.sock`.

If you use Docker Desktop on Arch Linux, the system `docker.service` and `docker` group may not exist. Start the Docker Desktop user service and use its context:

```bash
systemctl --user enable --now docker-desktop
docker context use desktop-linux
docker version
```

Docker Desktop exposes its engine at `~/.docker/desktop/docker.sock`; `cashlyctl system inspect-host` understands that socket through the active Docker context.

## Hotkey Limitation

This image is suitable for CLI operations, pairing, diagnostics, command dispatch, and the Textual console. It should not be treated as the primary global-hotkey runtime. Host-level hotkeys require a native host process or desktop shortcut integration, especially on Windows, macOS, and Linux Wayland.

Use host inspection to make the limitation visible:

```bash
docker run --rm cashlyctl:local system inspect-host
docker run --rm -e CASHLYCTL_HOST_OS=linux cashlyctl:local system inspect-host
```

On a native Arch Linux install, `cashlyctl system inspect-host` should report `host_os=linux`. On a Dockerized run, it may report low-confidence host detection unless `CASHLYCTL_HOST_OS` is provided.

To show the host OS in the console header from inside Docker, pass the host hint:

```bash
docker run --rm -it \
  -e CASHLYCTL_HOST_OS=linux \
  -v cashlyctl-state:/home/cashly/.cashlyctl \
  cashlyctl:local console
```
