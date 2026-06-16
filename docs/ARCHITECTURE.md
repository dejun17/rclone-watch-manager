# Architecture

Rclone Watch Manager has four main layers:

## 1. Registry

The registry defines watched folders and their remote destinations.

## 2. Shared rclone config

All watcher containers mount the same rclone config directory.

## 3. Docker watcher image

The embedded Dockerfile builds a small image based on `rclone/rclone:latest` with `inotify-tools`.

## 4. One container per watcher

Each watched folder gets a generated Docker Compose stack and container.

```text
Registry entry
  -> generated docker-compose.yml
  -> watcher container
  -> inotifywait
  -> rclone copy/sync
```
