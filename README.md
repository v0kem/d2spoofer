# Steam Cleanup Tool

[Русская документация](README_RU.md)

A Windows utility that previews and optionally removes selected Steam files and
matching Registry records. It is independent and is not affiliated with Valve
Corporation or Steam.

> **Destructive software:** real mode permanently deletes files and Registry
> data. The distributed configuration always starts with `"dry_run": true`.

## Features

- scans key names, value names, and value data across configured Registry hives;
- supports independent queries, exact/partial/regex matching, and exclusions;
- compacts binary Registry values in logs instead of printing `\x00` noise;
- keeps `steamapps` and `steam.exe` while cleaning other top-level Steam items;
- exports affected Registry hives before any deletion;
- stops Steam after confirmation and restarts `steam.exe` when cleanup finishes;
- validates dangerous paths, kept items, process names, and configuration typos;
- runs from source or as a standalone Windows EXE.

## Recommended release usage

1. Download `SteamCleanupTool-Windows.zip` from Releases and extract it.
2. Open `config.json` with `edit_config.bat` and keep `"dry_run": true`.
3. Optionally run `check_config.bat` to validate settings and paths instantly.
4. Double-click `run_cleanup.bat`; approve the Administrator prompt.
5. Review the complete console preview and `cleanup_log.txt`.
6. Only when every target is correct, change `"dry_run"` to `false`.
7. Run again. After the preview, cleanup starts automatically when the
   15-second countdown finishes.

No process is stopped and nothing is changed during a dry run. In real mode the
order is: preview → countdown → Registry backups → stop Steam → delete folder
items → delete Registry targets → restart Steam.

Close the window or press `Ctrl+C` during the countdown to cancel. To restore
manual confirmation, set `confirmation.mode` to `type_delete`.

Removing the Steam client files causes `steam.exe` to repair or download the
client again. The configured `steamapps` folder is preserved, but backups are
still strongly recommended.

## Configuration

All user settings are in `config.json` next to the EXE or Python script.
Relative log and backup paths are resolved relative to that JSON file.

Important settings:

- `dry_run`: `true` for preview, `false` to permit confirmed deletion;
- `confirmation.mode`: automatic `countdown` or manual `type_delete`;
- `confirmation.countdown_seconds`: delay before automatic cleanup, initially 15;
- `registry.search_terms`: independent queries, initially `dota`, `steam`, `valve`;
- `registry.exclude_terms`: case-insensitive false-positive exclusions such as `msteams`;
- `registry.exclude_path_suffixes`: optional Registry branch suffix exclusions;
- `registry.match_type`: `exact`, `partial`, or `regex`;
- `folder.path`: absolute Steam installation path;
- `folder.keep_folder` and `folder.keep_file`: exact top-level names to preserve;
- `steam_processes`: process stopping, timeout, force-stop, and restart settings.

An empty `steam_processes.executable` means `<folder.path>\steam.exe`. Unknown
JSON keys are rejected to prevent a misspelled safety option from being ignored.

## Running from source

Requirements: Windows 10/11 and Python 3.9 or newer; runtime dependencies are
not required.

```powershell
py -3 cleanup_tool.py --config config.json
```

To create a missing safe configuration:

```powershell
py -3 cleanup_tool.py --config config.json --create-config
```

To validate settings without scanning the Registry:

```powershell
py -3 cleanup_tool.py --config config.json --check-config
```

## Building the EXE

Run `build_exe.bat`. It creates an isolated `.build-venv`, installs PyInstaller,
and produces `SteamCleanupTool-Windows.zip`. Building requires internet access
for the build dependency; the resulting utility has no Python dependency.

The included GitHub Actions workflow compiles the EXE on every push/PR, uploads
the Windows archive as an artifact, and attaches it to tags such as `v1.1.0`.

All files must be placed directly in the repository root. In particular, the
workflow path must be exactly `.github/workflows/build-windows.yml`, not
`outputs/.github/workflows/build-windows.yml`, and the source ZIP must be
extracted rather than uploaded as one opaque ZIP file. If Actions are disabled
for the repository, enable them under **Settings → Actions → General**.

## Exit codes

- `0`: success, preview, or user cancellation;
- `1`: cleanup or Steam restart completed with errors;
- `2`: configuration or launcher error;
- `3`: required Registry backup failed, so cleanup was aborted;
- `4`: Steam processes could not be stopped, so cleanup was aborted.

## Development

```powershell
python -m py_compile cleanup_tool.py
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and the
[MIT License](LICENSE).
