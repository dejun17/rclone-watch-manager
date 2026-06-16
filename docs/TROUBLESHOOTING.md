# Troubleshooting

## Docker is not reachable

```bash
docker info
docker compose version
```

Start Docker:

```bash
sudo systemctl enable --now docker
```

## rclone remote test fails

```bash
rclone listremotes
rclone lsd Drive:
```

If needed, re-run rclone setup from the application menu.

## Watcher starts then exits

Common causes:

- Missing local folder
- Bad rclone remote name
- Expired OAuth token
- Permission issue on the mounted folder
- Docker cannot access the configured path

Use the log viewer in the app menu.

## Remote deletions happened unexpectedly

You probably used `sync` mode. For safer upload-only behavior, use `copy` mode.
