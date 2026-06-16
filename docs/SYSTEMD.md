# systemd Integration

Rclone Watch Manager can generate wrapper units that call Docker Compose for each watcher.

Generate units from the app menu, then install them manually:

```bash
sudo cp ~/.rclone-watch-manager/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rclone-watch-example.service
```

Docker's own restart policy is still used by generated containers.
