# Contributing

1. Keep `config.json` in safe preview mode.
2. Run `python -m py_compile cleanup_tool.py` and the test suite before a pull request.
3. Add tests for changes to deletion, path validation, registry matching, or process management.
4. Never commit generated logs, registry backups, build folders, or local configuration secrets.

Bug reports should include the utility version, Windows version, configuration
with private paths removed, and the smallest relevant log excerpt.
