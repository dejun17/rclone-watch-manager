# Backups

Rclone Watch Manager can export a backup archive containing:

- registry
- settings
- status cache
- shared rclone config
- generated watcher stacks
- Docker template files

## Security Warning

Backup archives may contain rclone OAuth tokens and private local paths.

Do not upload backup archives to GitHub unless you have inspected and sanitized them.
