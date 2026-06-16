# Security Policy

## Sensitive Files

Rclone Watch Manager may create files that contain secrets or private metadata.

Do not publish:

- `rclone.conf`
- backup archives
- generated logs
- generated Docker Compose stacks with private paths
- `~/.rclone-watch-manager/`

## Reporting Vulnerabilities

Please report security issues privately before public disclosure.

## Backup Warning

Backup archives can contain rclone OAuth tokens. Treat them like passwords.
