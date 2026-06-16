# Configuration

Rclone Watch Manager uses one shared rclone config for all watcher containers.

Default config location:

```text
~/.rclone-watch-manager/rclone-config/rclone.conf
```

The default remote name is:

```text
Drive
```

## Recommended Flow

1. Run the app.
2. Complete the first-run wizard.
3. Run rclone configuration setup.
4. Add a watched folder.
5. Run a dry-run test.
6. Start the watcher.

## Local Paths

Local paths are mounted into watcher containers as:

```text
/data/to-sync
```

## Remote Paths

Remote destinations use this format:

```text
RemoteName:path/from/remote/root
```
