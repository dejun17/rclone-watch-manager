# Contributing

Contributions are welcome.

## Development Checks

```bash
python -m py_compile src/rclone_watch_manager.py
python -m unittest discover tests
```

## Pull Request Expectations

- Keep changes focused.
- Avoid committing generated app data.
- Update documentation when behavior changes.
- Do not include real rclone configs, tokens, paths, or backup archives.
