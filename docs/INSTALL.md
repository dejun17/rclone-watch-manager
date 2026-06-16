# Installation

## Ubuntu/Debian

```bash
sudo apt-get update
sudo apt-get install -y python3 docker.io docker-compose-plugin rclone
sudo systemctl enable --now docker
```

Optional Docker group access:

```bash
sudo usermod -aG docker "$USER"
```

Log out and back in after changing group membership.

## Run

```bash
python3 src/rclone_watch_manager.py
```

The first-run wizard checks dependencies, helps configure rclone, and builds the shared watcher image.
