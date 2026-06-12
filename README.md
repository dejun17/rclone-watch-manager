# Rclone Watch Manager

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)]()
[![Docker](https://img.shields.io/badge/Docker-Required-blue.svg)]()
[![License](https://img.shields.io/badge/License-MIT-green.svg)]()

Rclone Watch Manager is a menu-driven operations console that automatically monitors local folders and synchronizes them to cloud storage using Docker, rclone, and inotify.

## Features

- Multi-folder watcher management
- Shared rclone configuration
- Docker-powered isolation
- Health dashboard
- Dry-run validation
- Backup & restore
- Log analysis
- Systemd unit generation
- Google Drive and any rclone-supported backend

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/rclone-watch-manager.git
cd rclone-watch-manager
python3 src/rclone_watch_manager.py
```

## Security

Backups may contain rclone credentials and OAuth tokens. Never commit generated application data or backup archives.

## License

MIT
