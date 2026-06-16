# Rclone Watch Manager

[![Python Check](https://github.com/YOUR_USERNAME/rclone-watch-manager/actions/workflows/python-check.yml/badge.svg)](https://github.com/YOUR_USERNAME/rclone-watch-manager/actions/workflows/python-check.yml)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)]()
[![Docker](https://img.shields.io/badge/Docker-required-blue.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)]()

**Rclone Watch Manager** is a menu-driven operations console for watching local folders and synchronizing them to cloud storage using **Docker**, **rclone**, and Linux **inotify**.

It is built for homelab users, Linux admins, and anyone who wants a recoverable, explicit, easy-to-control folder watcher instead of a pile of mystery cron jobs.

## Features

- Add, edit, remove, start, and stop folder watchers
- One Docker container per watched folder
- Shared rclone configuration for all watchers
- `copy` mode by default for safer upload-only behavior
- Optional `sync` mode for true mirror behavior
- Dry-run validation before starting watchers
- Health dashboard
- Log viewer and live log follow
- Backup and restore for settings, registry, generated stacks, and rclone config
- systemd wrapper generation
- Works with Google Drive and any rclone-supported backend

## Requirements

- Linux recommended
- Python 3.9+
- Docker
- Docker Compose plugin
- rclone

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/rclone-watch-manager.git
cd rclone-watch-manager
python3 src/rclone_watch_manager.py
```

Show version:

```bash
python3 src/rclone_watch_manager.py --version
```

Run validation only:

```bash
python3 src/rclone_watch_manager.py --validate
```

## How It Works

```text
Local folder
  -> Docker watcher container
  -> inotify event detection
  -> rclone copy/sync
  -> cloud storage remote
```

## Default Data Directory

```text
~/.rclone-watch-manager
```

Do **not** commit this directory.

## Security Warning

Backups may include `rclone.conf`, OAuth tokens, remote credentials, generated stacks, and local path metadata.

Never commit:

- `~/.rclone-watch-manager/`
- `rclone.conf`
- backup `.tar.gz` archives
- generated logs
- generated stack files containing private local paths

## Sync Modes

`copy` mode is the default and safest option. It uploads and updates files but does not delete remote files.

`sync` mode mirrors the local folder to the remote destination and can delete files remotely. Use it only when you understand the risk.

## License

MIT License.
