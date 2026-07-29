#!/usr/bin/env python3
"""
Safe registry + folder cleanup tool for Windows.

The program always builds and prints a preview first. Real deletion requires:
  1) "dry_run": false in the external config.json;
  2) completing the configured countdown or typing DELETE in legacy mode.

Registry terminology:
  * a key has a name and contains values;
  * every value has a name (possibly empty for the default value) and data.
The search covers key names, value names and value data.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import ntpath
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import winreg
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Pattern, Tuple


# ============================================================================
# DEFAULT CONFIGURATION — users edit config.json, not this file
# ============================================================================

CONFIG = {
    # Keep True for the first run. False enables the confirmation mode below.
    "dry_run": True,
    "log_file": "cleanup_log.txt",

    "confirmation": {
        "mode": "countdown",       # countdown | type_delete
        "countdown_seconds": 15,
    },

    "registry": {
        "enabled": True,
        # Every root shown by Registry Editor. An empty start_path means the
        # whole hive. Some roots overlap (HKCR/HKCC/HKCU are mapped views),
        # but listing all five gives the expected "entire registry" view.
        "targets": [
            {"hive": "HKCR", "start_path": ""},
            {"hive": "HKCU", "start_path": ""},
            {"hive": "HKLM", "start_path": ""},
            {"hive": "HKU", "start_path": ""},
            {"hive": "HKCC", "start_path": ""},
        ],
        # Must be True when any target starts at a hive root. This explicit
        # switch prevents an accidental root scan after a typo in start_path.
        "allow_full_hive_scan": True,
        "search_terms": ["dota", "steam", "valve"],
        # Case-insensitive substrings that suppress false positives. A matching
        # key name skips its whole subtree; matching value names/data are also
        # omitted from both the preview and deletion plan.
        "exclude_terms": ["msteams"],
        # Optional registry path suffixes to skip with every child key/value.
        # ComDlg32 is intentionally not excluded: its matching entries remain
        # visible, but binary data is compacted in the preview below.
        "exclude_path_suffixes": [],
        "match_type": "partial",     # exact | partial | regex
        "recursive": True,
        "delete_matching_keys": True,
        "delete_matching_values": True,
        # A failed backup prevents registry deletion.
        "backup_before_delete": True,
        "backup_dir": "registry_backups",
    },

    "folder": {
        "enabled": True,
        "path": r"C:\Program Files (x86)\Steam",
        # Exact top-level names, case-insensitive. Use None to keep no such item.
        "keep_folder": "steamapps",
        "keep_file": "steam.exe",
    },

    "steam_processes": {
        "enabled": True,
        "stop_before_cleanup": True,
        "restart_after_cleanup": True,
        # Empty means <folder.path>\steam.exe.
        "executable": "",
        "process_names": [
            "steam.exe",
            "steamwebhelper.exe",
            "steamerrorreporter.exe",
            "steamerrorreporter64.exe",
            "gameoverlayui.exe",
            "gameoverlayui64.exe",
        ],
        "graceful_timeout_seconds": 10,
        "force_kill_after_timeout": True,
    },
}

# This override exists for exceptional, deliberate use. Leave it False.
ALLOW_DANGEROUS_FOLDER = False

APP_NAME = "Steam Cleanup Tool"
APP_VERSION = "1.1.0"


# ============================================================================
# COMMON HELPERS
# ============================================================================

HIVE_NAMES = {  
    winreg.HKEY_CLASSES_ROOT: "HKCR",
    winreg.HKEY_CURRENT_USER: "HKCU",
    winreg.HKEY_LOCAL_MACHINE: "HKLM",
    winreg.HKEY_USERS: "HKU",
    winreg.HKEY_CURRENT_CONFIG: "HKCC",
}

HIVE_HANDLES = {name: handle for handle, name in HIVE_NAMES.items()}


class ConfigurationError(ValueError):
    """Raised when a configuration could make the operation unsafe."""


def application_directory() -> Path:
    """Directory containing the script or the frozen executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_config_path() -> Path:
    return application_directory() / "config.json"


def _merge_known_config(defaults: dict, supplied: dict, location: str = "config") -> dict:
    if not isinstance(supplied, dict):
        raise ConfigurationError(f"{location} must be a JSON object")
    result = copy.deepcopy(defaults)
    for key, value in supplied.items():
        if key not in defaults:
            raise ConfigurationError(f"unknown setting: {location}.{key}")
        if isinstance(defaults[key], dict):
            result[key] = _merge_known_config(defaults[key], value, f"{location}.{key}")
        else:
            result[key] = value
    return result


def _resolve_relative_file(value: object, base_dir: Path, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve(strict=False))


def load_config_file(path: Path) -> dict:
    path = path.expanduser().resolve(strict=False)
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            supplied = json.load(stream)
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"configuration file not found: {path}. Run with --create-config first."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"could not read configuration {path}: {exc}") from exc

    config = _merge_known_config(CONFIG, supplied)
    config["log_file"] = _resolve_relative_file(
        config["log_file"], path.parent, "log_file"
    )
    registry = config["registry"]
    registry["backup_dir"] = _resolve_relative_file(
        registry["backup_dir"], path.parent, "registry.backup_dir"
    )
    return config


def write_default_config(path: Path) -> None:
    path = path.expanduser().resolve(strict=False)
    if path.exists():
        raise ConfigurationError(f"refusing to overwrite existing configuration: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as stream:
            json.dump(CONFIG, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except OSError as exc:
        raise ConfigurationError(f"could not create configuration {path}: {exc}") from exc


def validate_config_types(config: dict) -> None:
    if not isinstance(config, dict):
        raise ConfigurationError("config must be an object")
    if not isinstance(config.get("dry_run"), bool):
        raise ConfigurationError("dry_run must be true or false")
    if not isinstance(config.get("log_file"), str) or not config["log_file"].strip():
        raise ConfigurationError("log_file must be a non-empty path string")

    confirmation = config.get("confirmation")
    if confirmation is not None:
        if not isinstance(confirmation, dict):
            raise ConfigurationError("confirmation must be an object")
        if confirmation.get("mode") not in {"countdown", "type_delete"}:
            raise ConfigurationError(
                "confirmation.mode must be countdown or type_delete"
            )
        seconds = confirmation.get("countdown_seconds")
        if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= 300:
            raise ConfigurationError(
                "confirmation.countdown_seconds must be an integer from 1 to 300"
            )

    for section_name in ("registry", "folder"):
        section = config.get(section_name)
        if not isinstance(section, dict):
            raise ConfigurationError(f"{section_name} must be an object")
        if not isinstance(section.get("enabled"), bool):
            raise ConfigurationError(f"{section_name}.enabled must be true or false")

    registry = config["registry"]
    if registry["enabled"]:
        for name in (
            "allow_full_hive_scan",
            "recursive",
            "delete_matching_keys",
            "delete_matching_values",
            "backup_before_delete",
        ):
            if not isinstance(registry.get(name), bool):
                raise ConfigurationError(f"registry.{name} must be true or false")

    folder = config["folder"]
    if folder["enabled"]:
        if not isinstance(folder.get("path"), str) or not folder["path"].strip():
            raise ConfigurationError("folder.path must be a non-empty path string")

    processes = config.get("steam_processes", {"enabled": False})
    if not isinstance(processes, dict) or not isinstance(processes.get("enabled"), bool):
        raise ConfigurationError("steam_processes.enabled must be true or false")


class Logger:
    def __init__(self, log_file: str):
        self.path = Path(log_file).expanduser()
        self.lines: List[str] = []

    def log(self, message: str = "") -> None:
        print(message)
        self.lines.append(message)

    def save(self) -> None:
        try:
            if self.path.parent != Path("."):
                self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write("\n" + "=" * 72 + "\n")
                stream.write(f"Run at {datetime.now().isoformat(timespec='seconds')}\n")
                stream.write("=" * 72 + "\n")
                stream.write("\n".join(self.lines) + "\n")
        except OSError as exc:
            print(f"WARNING: could not save log to {self.path}: {exc}", file=sys.stderr)


def registry_join(path: str, name: str) -> str:
    return f"{path}\\{name}" if path else name


def format_registry_data(value: object, max_characters: int = 240) -> str:
    """Return a compact, single-line representation for preview/log output."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary data: {len(value)} bytes>"

    rendered = repr(value).replace("\r", r"\r").replace("\n", r"\n")
    if len(rendered) <= max_characters:
        return rendered
    return f"{rendered[:max_characters]}... <{len(rendered)} characters total>"


def open_registry_key(hive, path: str, access: int):
    return winreg.OpenKey(hive, path if path else None, 0, access)


def validate_leaf_name(name: Optional[str], label: str) -> None:
    if name is None:
        return
    if not isinstance(name, str) or not name.strip():
        raise ConfigurationError(f"{label} must be a non-empty name or None")
    if name in {".", ".."} or ntpath.basename(name) != name or "/" in name or "\\" in name:
        raise ConfigurationError(f"{label} must be a top-level name, not a path: {name!r}")


# ============================================================================
# REGISTRY CLEANER
# ============================================================================

class Matcher:
    def __init__(
        self, terms, match_type: str, exclude_terms=None, exclude_path_suffixes=None
    ):
        # Accept one string for backwards-compatible programmatic use, while
        # CONFIG uses a list of independent search queries.
        if isinstance(terms, str):
            terms = [terms]
        if not isinstance(terms, list) or not terms:
            raise ConfigurationError("registry.search_terms must be a non-empty list")
        if any(not isinstance(term, str) or not term for term in terms):
            raise ConfigurationError("every registry search term must be a non-empty string")
        folded = [term.casefold() for term in terms]
        if len(set(folded)) != len(folded):
            raise ConfigurationError("registry.search_terms contains duplicates")
        if match_type not in {"exact", "partial", "regex"}:
            raise ConfigurationError("registry.match_type must be exact, partial, or regex")
        if exclude_terms is None:
            exclude_terms = []
        if not isinstance(exclude_terms, list):
            raise ConfigurationError("registry.exclude_terms must be a list")
        if any(not isinstance(term, str) or not term for term in exclude_terms):
            raise ConfigurationError(
                "every registry exclusion term must be a non-empty string"
            )
        folded_exclusions = [term.casefold() for term in exclude_terms]
        if len(set(folded_exclusions)) != len(folded_exclusions):
            raise ConfigurationError("registry.exclude_terms contains duplicates")
        if exclude_path_suffixes is None:
            exclude_path_suffixes = []
        if not isinstance(exclude_path_suffixes, list):
            raise ConfigurationError("registry.exclude_path_suffixes must be a list")
        normalized_suffixes = []
        for suffix in exclude_path_suffixes:
            if not isinstance(suffix, str) or not suffix.strip("\\/"):
                raise ConfigurationError(
                    "every registry excluded path suffix must be a non-empty string"
                )
            normalized = suffix.replace("/", "\\").strip("\\").casefold()
            normalized_suffixes.append(normalized)
        if len(set(normalized_suffixes)) != len(normalized_suffixes):
            raise ConfigurationError("registry.exclude_path_suffixes contains duplicates")
        self.terms: List[str] = terms
        self.exclude_terms: List[str] = exclude_terms
        self.exclude_path_suffixes: List[str] = normalized_suffixes
        self.kind = match_type
        self.regexes: Dict[str, Pattern[str]] = {}
        if match_type == "regex":
            for term in terms:
                try:
                    self.regexes[term] = re.compile(term, re.IGNORECASE)
                except re.error as exc:
                    raise ConfigurationError(f"invalid registry regex {term!r}: {exc}") from exc

    def matches(self, value: object) -> bool:
        return bool(self.matching_terms(value))

    def is_excluded(self, value: object) -> bool:
        if isinstance(value, (list, tuple)):
            return any(self.is_excluded(item) for item in value)
        folded_text = str(value).casefold()
        return any(term.casefold() in folded_text for term in self.exclude_terms)

    def path_is_excluded(self, path: str) -> bool:
        normalized = path.replace("/", "\\").strip("\\").casefold()
        return any(
            normalized == suffix or normalized.endswith("\\" + suffix)
            for suffix in self.exclude_path_suffixes
        )

    def matching_terms(self, value: object) -> List[str]:
        if self.is_excluded(value):
            return []
        # REG_MULTI_SZ is returned as a list of strings. Treat every entry as
        # searchable data so an exact match means an exact list element, not
        # Python's textual representation of the whole list.
        if isinstance(value, (list, tuple)):
            found = []
            for item in value:
                for term in self.matching_terms(item):
                    if term not in found:
                        found.append(term)
            return found
        text = str(value)
        if self.kind == "exact":
            return [term for term in self.terms if text.casefold() == term.casefold()]
        if self.kind == "partial":
            folded_text = text.casefold()
            return [term for term in self.terms if term.casefold() in folded_text]
        return [term for term in self.terms if self.regexes[term].search(text) is not None]


class RegistryCleaner:
    """Builds a deletion plan without modifying the registry."""

    def __init__(self, logger: Logger, delete_keys: bool, delete_values: bool):
        self.logger = logger
        self.delete_keys = delete_keys
        self.delete_values = delete_values
        self.matches: Dict[Tuple[str, str, str], dict] = {}
        self.denied_paths = 0
        self.scanned_keys = 0
        self.excluded_branches = 0
        self.excluded_values = 0

    def find(
        self,
        hive,
        start_path: str,
        matcher: Matcher,
        recursive: bool,
    ) -> None:
        self.matches.clear()
        self.denied_paths = 0
        self.scanned_keys = 0
        self.excluded_branches = 0
        self.excluded_values = 0
        start_path = start_path.strip("\\")

        if start_path and (matcher.is_excluded(start_path) or matcher.path_is_excluded(start_path)):
            self.excluded_branches = 1
            self.logger.log("Registry target was skipped by registry.exclude_terms.")
            return

        # If the selected starting key itself matches, plan its deletion and do
        # not enumerate descendants: they are already covered by that deletion.
        if start_path and self.delete_keys and matcher.matches(ntpath.basename(start_path)):
            parent, _, name = start_path.rpartition("\\")
            self.matches[("key", parent, name)] = {
                "type": "key", "path": parent, "name": name, "reasons": ["name"],
                "terms": matcher.matching_terms(name),
            }
            self.logger.log("Registry starting key matches; its whole subtree is covered.")
            return

        pending = [start_path]
        while pending:
            path = pending.pop()
            try:
                key = open_registry_key(hive, path, winreg.KEY_READ)
            except FileNotFoundError:
                if path == start_path:
                    self.logger.log(f"Registry path not found: {path or '<hive root>'}")
                continue
            except PermissionError:
                self.denied_paths += 1
                # A full-hive scan can encounter thousands of protected keys.
                # Keep the log useful instead of printing every inaccessible path.
                if self.denied_paths <= 20:
                    self.logger.log(f"  permission denied: {path or '<hive root>'}")
                continue

            try:
                self.scanned_keys += 1
                if self.delete_values:
                    self._scan_values(key, path, matcher)
                if recursive:
                    self._scan_subkeys(key, path, matcher, pending)
            finally:
                winreg.CloseKey(key)

        self.logger.log(
            f"\nRegistry: {self.scanned_keys} key(s) scanned, "
            f"{len(self.matches)} deletion target(s) found"
        )
        if self.denied_paths:
            self.logger.log(f"Registry: {self.denied_paths} inaccessible path(s) skipped")
        if self.excluded_branches or self.excluded_values:
            self.logger.log(
                "Registry exclusions: "
                f"{self.excluded_branches} branch(es), "
                f"{self.excluded_values} value(s) skipped"
            )

    def _scan_values(self, key, path: str, matcher: Matcher) -> None:
        index = 0
        while True:
            try:
                value_name, value_data, _value_type = winreg.EnumValue(key, index)
            except OSError:
                break
            if matcher.is_excluded(value_name) or matcher.is_excluded(value_data):
                self.excluded_values += 1
                index += 1
                continue
            reasons, terms = [], []
            name_terms = matcher.matching_terms(value_name)
            data_terms = matcher.matching_terms(value_data)
            if name_terms:
                reasons.append("name")
                terms.extend(name_terms)
            if data_terms:
                reasons.append("data")
                terms.extend(term for term in data_terms if term not in terms)
            if reasons:
                self.matches[("value", path, value_name)] = {
                    "type": "value",
                    "path": path,
                    "name": value_name,
                    "data": value_data,
                    "reasons": reasons,
                    "terms": terms,
                }
            index += 1

    def _scan_subkeys(
        self,
        key,
        path: str,
        matcher: Matcher,
        pending: List[str],
    ) -> None:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
            except OSError:
                break
            child_path = registry_join(path, name)
            if matcher.is_excluded(name) or matcher.path_is_excluded(child_path):
                self.excluded_branches += 1
                index += 1
                continue
            matched_terms = matcher.matching_terms(name)
            if self.delete_keys and matched_terms:
                self.matches[("key", path, name)] = {
                    "type": "key", "path": path, "name": name, "reasons": ["name"],
                    "terms": matched_terms,
                }
                # No need to scan inside a key that will be deleted wholesale.
            else:
                pending.append(child_path)
            index += 1

    def preview(self) -> None:
        if not self.matches:
            self.logger.log("  (no registry targets)")
            return
        for item in sorted(
            self.matches.values(),
            key=lambda entry: (entry["path"].casefold(), entry["name"].casefold(), entry["type"]),
        ):
            reasons = "+".join(item["reasons"])
            queries = ", ".join(item.get("terms", []))
            display_name = item["name"] if item["name"] else "(Default)"
            full_name = registry_join(item["path"], display_name)
            if item["type"] == "key":
                self.logger.log(
                    f"  [KEY:{reasons}; query={queries}] {full_name}  (entire subtree)"
                )
            else:
                self.logger.log(
                    f"  [VALUE:{reasons}; query={queries}] {full_name} = "
                    f"{format_registry_data(item.get('data'))}"
                )

    def delete_all(self, hive) -> Tuple[int, int]:
        deleted = failed = 0
        values = [item for item in self.matches.values() if item["type"] == "value"]
        keys = [item for item in self.matches.values() if item["type"] == "key"]

        for item in values:
            try:
                with open_registry_key(hive, item["path"], winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, item["name"])
                self.logger.log(
                    f"  deleted registry value: {registry_join(item['path'], item['name'] or '(Default)')}"
                )
                deleted += 1
            except FileNotFoundError:
                self.logger.log("  skipped registry value that no longer exists")
            except OSError as exc:
                self.logger.log(f"  FAILED registry value {item['path']}\\{item['name']}: {exc}")
                failed += 1

        # Deepest keys first makes the plan deterministic even if it changes later.
        keys.sort(key=lambda item: registry_join(item["path"], item["name"]).count("\\"), reverse=True)
        for item in keys:
            full_path = registry_join(item["path"], item["name"])
            try:
                self._delete_key_tree(hive, full_path)
                self.logger.log(f"  deleted registry key: {full_path}")
                deleted += 1
            except FileNotFoundError:
                self.logger.log(f"  skipped missing registry key: {full_path}")
            except OSError as exc:
                self.logger.log(f"  FAILED registry key {full_path}: {exc}")
                failed += 1

        self.logger.log(f"\nRegistry: {deleted} deleted, {failed} failed")
        return deleted, failed

    def _delete_key_tree(self, hive, path: str) -> None:
        with open_registry_key(hive, path, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                self._delete_key_tree(hive, registry_join(path, child))

        parent_path, separator, name = path.rpartition("\\")
        if not separator and not name:
            raise ConfigurationError("refusing to delete a registry hive root")
        with open_registry_key(hive, parent_path, winreg.KEY_ALL_ACCESS) as parent:
            winreg.DeleteKey(parent, name or path)


def validate_registry_config(config: dict) -> Tuple[Matcher, List[Tuple[object, str]]]:
    raw_targets = config.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigurationError("registry.targets must contain at least one hive/path")

    targets: List[Tuple[object, str]] = []
    seen = set()
    for index, target in enumerate(raw_targets, start=1):
        if not isinstance(target, dict):
            raise ConfigurationError(f"registry.targets[{index}] must be a dictionary")
        hive = target.get("hive")
        if isinstance(hive, str):
            hive = HIVE_HANDLES.get(hive.upper())
        start_path = target.get("start_path")
        if hive not in HIVE_NAMES:
            raise ConfigurationError(f"registry.targets[{index}].hive is unsupported")
        if not isinstance(start_path, str):
            raise ConfigurationError(f"registry.targets[{index}].start_path must be a string")
        normalized_path = start_path.strip("\\")
        if not normalized_path and not config.get("allow_full_hive_scan", False):
            raise ConfigurationError(
                "a hive-root target requires registry.allow_full_hive_scan = True"
            )
        identity = (hive, normalized_path.casefold())
        if identity in seen:
            raise ConfigurationError(f"duplicate registry target: {HIVE_NAMES[hive]}\\{normalized_path}")
        seen.add(identity)
        targets.append((hive, normalized_path))

    if not config["delete_matching_keys"] and not config["delete_matching_values"]:
        raise ConfigurationError("both registry deletion target types are disabled")
    return Matcher(
        config["search_terms"],
        config["match_type"],
        config.get("exclude_terms", []),
        config.get("exclude_path_suffixes", []),
    ), targets


def backup_registry(hive, path: str, backup_dir: str, logger: Logger) -> bool:
    directory = Path(backup_dir).expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.log(f"Registry backup directory could not be created: {exc}")
        return False

    hive_name = HIVE_NAMES[hive]
    safe_path = re.sub(r'[\\/:*?"<>|]', "_", path) or "ROOT"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = directory / f"{hive_name}_{safe_path}_{stamp}.reg"
    source = f"{hive_name}\\{path}" if path else hive_name

    try:
        result = subprocess.run(
            ["reg.exe", "export", source, str(destination), "/y"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.log(f"Registry backup failed: {exc}")
        return False

    if result.returncode != 0 or not destination.is_file():
        detail = (result.stderr or result.stdout).strip()
        logger.log(f"Registry backup failed: {detail or 'unknown reg.exe error'}")
        return False
    logger.log(f"Registry backup saved: {destination.resolve()}")
    return True


# ============================================================================
# FOLDER CLEANER
# ============================================================================

def _same_or_descendant(candidate: str, root: str) -> bool:
    try:
        return ntpath.commonpath([candidate, root]).casefold() == ntpath.normpath(root).casefold()
    except ValueError:
        return False


def folder_is_dangerous(path: Path) -> bool:
    normalized = ntpath.normpath(str(path)).casefold()
    drive, tail = ntpath.splitdrive(normalized)
    if drive and not tail.strip("\\/"):
        return True

    windows_dir = os.environ.get("WINDIR", r"C:\\Windows")
    program_files = [
        os.environ.get("ProgramFiles", r"C:\\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)"),
    ]
    windows_normalized = ntpath.normpath(windows_dir).casefold()
    if _same_or_descendant(normalized, windows_normalized):
        return True

    # Block Program Files itself, while permitting a deliberately selected
    # application subfolder such as ...\Program Files (x86)\Steam.
    if normalized in {
        ntpath.normpath(critical).casefold() for critical in program_files
    }:
        return True

    exact_critical = [r"C:\\Users", r"C:\\ProgramData"]
    profile = os.environ.get("USERPROFILE")
    if profile:
        exact_critical.append(profile)
    return normalized in {ntpath.normpath(item).casefold() for item in exact_critical}


class FolderCleaner:
    def __init__(self, logger: Logger):
        self.logger = logger
        self.root: Optional[Path] = None
        self.to_delete: List[Path] = []
        self.kept: List[Path] = []

    def prepare(
        self,
        path_text: str,
        keep_folder: Optional[str],
        keep_file: Optional[str],
    ) -> None:
        validate_leaf_name(keep_folder, "folder.keep_folder")
        validate_leaf_name(keep_file, "folder.keep_file")
        configured = Path(path_text).expanduser()
        if not configured.is_absolute():
            raise ConfigurationError("folder.path must be an absolute path")
        if not configured.exists() or not configured.is_dir():
            raise ConfigurationError(f"folder.path is not an existing directory: {configured}")

        self.root = configured.resolve(strict=True)
        if folder_is_dangerous(self.root) and not ALLOW_DANGEROUS_FOLDER:
            raise ConfigurationError(f"critical folder is blocked: {self.root}")

        children = list(self.root.iterdir())
        by_name: Dict[str, List[Path]] = {}
        for child in children:
            by_name.setdefault(child.name.casefold(), []).append(child)

        self.kept = []
        if keep_folder is not None:
            matches = by_name.get(keep_folder.casefold(), [])
            if len(matches) != 1 or not matches[0].is_dir():
                raise ConfigurationError(
                    f"folder.keep_folder was not found as one top-level folder: {keep_folder!r}; "
                    "folder cleanup is cancelled"
                )
            self.kept.append(matches[0])
        if keep_file is not None:
            matches = by_name.get(keep_file.casefold(), [])
            if len(matches) != 1 or not matches[0].is_file():
                raise ConfigurationError(
                    f"folder.keep_file was not found as one top-level file: {keep_file!r}; "
                    "folder cleanup is cancelled"
                )
            self.kept.append(matches[0])

        kept_keys = {str(item).casefold() for item in self.kept}
        self.to_delete = [
            item for item in children if str(item).casefold() not in kept_keys
        ]
        for item in self.kept:
            self.logger.log(f"  keeping: {item}")
        self.logger.log(f"\nFolder: {len(self.to_delete)} top-level item(s) will be removed")

    def preview(self) -> None:
        if not self.to_delete:
            self.logger.log("  (no folder targets)")
            return
        for item in sorted(self.to_delete, key=lambda value: value.name.casefold()):
            if item.is_symlink():
                kind = "LINK"
            elif _is_junction(item):
                kind = "JUNCTION"
            elif item.is_dir():
                kind = "DIR"
            else:
                kind = "FILE"
            self.logger.log(f"  [{kind}] {item}")

    def delete_all(self) -> Tuple[int, int]:
        if self.root is None:
            return 0, 0
        deleted = failed = 0
        for item in self.to_delete:
            # Re-check containment and direct parent immediately before removal.
            if item.parent != self.root:
                self.logger.log(f"  BLOCKED path outside selected folder: {item}")
                failed += 1
                continue
            try:
                remove_path_without_following_links(item)
                self.logger.log(f"  deleted: {item}")
                deleted += 1
            except FileNotFoundError:
                self.logger.log(f"  skipped item that no longer exists: {item}")
            except OSError as exc:
                self.logger.log(f"  FAILED folder item {item}: {exc}")
                failed += 1
        self.logger.log(f"\nFolder: {deleted} deleted, {failed} failed")
        return deleted, failed


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    return bool(checker and checker(path))


def _make_writable(function, path, _error_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_path_without_following_links(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif _is_junction(path):
        os.rmdir(path)
    elif path.is_dir():
        shutil.rmtree(path, onerror=_make_writable)
    else:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


# ============================================================================
# STEAM PROCESS MANAGEMENT
# ============================================================================

def validate_process_config(config: dict, folder_config: dict) -> Optional[Path]:
    if not isinstance(config, dict):
        raise ConfigurationError("steam_processes must be an object")
    for name in (
        "enabled",
        "stop_before_cleanup",
        "restart_after_cleanup",
        "force_kill_after_timeout",
    ):
        if not isinstance(config.get(name), bool):
            raise ConfigurationError(f"steam_processes.{name} must be true or false")

    names = config.get("process_names")
    if not isinstance(names, list) or not names:
        raise ConfigurationError("steam_processes.process_names must be a non-empty list")
    folded_names = []
    for name in names:
        validate_leaf_name(name, "steam_processes.process_names item")
        if not name.casefold().endswith(".exe"):
            raise ConfigurationError(f"process name must end with .exe: {name!r}")
        folded_names.append(name.casefold())
    if len(set(folded_names)) != len(folded_names):
        raise ConfigurationError("steam_processes.process_names contains duplicates")

    timeout = config.get("graceful_timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 0 <= timeout <= 120:
        raise ConfigurationError(
            "steam_processes.graceful_timeout_seconds must be an integer from 0 to 120"
        )

    executable_text = config.get("executable")
    if not isinstance(executable_text, str):
        raise ConfigurationError("steam_processes.executable must be a string")
    if executable_text:
        executable = Path(executable_text).expanduser()
        if not executable.is_absolute():
            executable = Path(folder_config["path"]) / executable
    else:
        executable = Path(folder_config["path"]) / "steam.exe"
    executable = executable.resolve(strict=False)

    if config["restart_after_cleanup"]:
        if not executable.is_file():
            raise ConfigurationError(f"Steam restart executable was not found: {executable}")
        if folder_config.get("enabled"):
            folder_root = Path(folder_config["path"]).resolve(strict=False)
            if executable.parent == folder_root:
                kept_file = folder_config.get("keep_file")
                if not kept_file or executable.name.casefold() != kept_file.casefold():
                    raise ConfigurationError(
                        "Steam restart executable would be removed by folder cleanup; "
                        "set folder.keep_file to its exact filename"
                    )
    return executable


def process_is_running(process_name: str) -> bool:
    try:
        result = subprocess.run(
            [
                "tasklist.exe", "/FI", f"IMAGENAME eq {process_name}",
                "/FO", "CSV", "/NH",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"could not query process {process_name}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise OSError(f"tasklist failed for {process_name}: {detail or result.returncode}")
    for row in csv.reader(result.stdout.splitlines()):
        if row and row[0].casefold() == process_name.casefold():
            return True
    return False


def _taskkill(process_name: str, force: bool) -> subprocess.CompletedProcess:
    command = ["taskkill.exe", "/IM", process_name, "/T"]
    if force:
        command.append("/F")
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def stop_steam_processes(config: dict, logger: Logger) -> Tuple[bool, bool]:
    """Stop configured processes. Returns (anything_was_running, success)."""
    names = config["process_names"]
    try:
        running = [name for name in names if process_is_running(name)]
    except OSError as exc:
        logger.log(f"Steam process check FAILED: {exc}")
        return False, False
    if not running:
        logger.log("Steam processes: none are running")
        return False, True

    logger.log("Steam processes detected: " + ", ".join(running))
    for name in running:
        try:
            _taskkill(name, force=False)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.log(f"  graceful stop failed for {name}: {exc}")

    deadline = time.monotonic() + config["graceful_timeout_seconds"]
    remaining = list(running)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.25)
        try:
            remaining = [name for name in remaining if process_is_running(name)]
        except OSError as exc:
            logger.log(f"Steam process verification FAILED: {exc}")
            return True, False

    if remaining and config["force_kill_after_timeout"]:
        logger.log("Force-stopping remaining Steam processes: " + ", ".join(remaining))
        for name in remaining:
            try:
                result = _taskkill(name, force=True)
            except (OSError, subprocess.SubprocessError) as exc:
                logger.log(f"  force stop failed for {name}: {exc}")
                continue
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                logger.log(f"  force stop failed for {name}: {detail or result.returncode}")
        try:
            remaining = [name for name in remaining if process_is_running(name)]
        except OSError as exc:
            logger.log(f"Steam process verification FAILED: {exc}")
            return True, False

    if remaining:
        logger.log("Steam processes still running: " + ", ".join(remaining))
        return True, False
    logger.log("Steam processes stopped successfully")
    return True, True


def restart_steam(executable: Path, logger: Logger) -> bool:
    try:
        creation_flags = 0
        if os.name == "nt":
            creation_flags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError as exc:
        logger.log(f"Steam restart FAILED: {exc}")
        return False
    logger.log(f"Steam started: {executable}")
    return True


# ============================================================================
# ORCHESTRATION
# ============================================================================

def confirm_execution(config: dict, logger: Logger) -> bool:
    confirmation = config.get(
        "confirmation",
        {"mode": "type_delete", "countdown_seconds": 15},
    )
    if confirmation["mode"] == "type_delete":
        return input("Type DELETE (all caps) to continue: ") == "DELETE"

    seconds = confirmation["countdown_seconds"]
    logger.log(
        f"Cleanup will start automatically in {seconds} seconds. "
        "Press Ctrl+C or close this window to cancel."
    )
    try:
        for remaining in range(seconds, 0, -1):
            print(
                f"\rStarting destructive cleanup in {remaining:3d} second(s)... ",
                end="",
                flush=True,
            )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\rCountdown cancelled.                          ")
        logger.log("Countdown cancelled by user.")
        return False
    print("\rCountdown complete. Starting cleanup.             ")
    logger.log("Countdown completed; automatic confirmation accepted.")
    return True


def main(config: Optional[dict] = None) -> int:
    active_config = CONFIG if config is None else config
    try:
        validate_config_types(active_config)
    except ConfigurationError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2
    logger = Logger(active_config["log_file"])
    registry_config = active_config["registry"]
    folder_config = active_config["folder"]
    process_config = active_config.get("steam_processes", {"enabled": False})

    if not registry_config["enabled"] and not folder_config["enabled"]:
        logger.log("Both operations are disabled. Edit config.json and enable the one(s) you need.")
        logger.save()
        return 0

    logger.log("=" * 72)
    logger.log("REGISTRY + FOLDER CLEANUP — PREVIEW")
    logger.log("=" * 72)

    registry_jobs: List[Tuple[object, str, RegistryCleaner]] = []
    folder_cleaner: Optional[FolderCleaner] = None
    restart_executable: Optional[Path] = None

    try:
        # Prepare and preview the folder first so the displayed order matches
        # the real execution order below.
        if folder_config["enabled"]:
            folder_cleaner = FolderCleaner(logger)
            logger.log(f"\nScanning folder: {folder_config['path']}")
            folder_cleaner.prepare(
                folder_config["path"],
                folder_config["keep_folder"],
                folder_config["keep_file"],
            )
            folder_cleaner.preview()

        if process_config.get("enabled", False):
            restart_executable = validate_process_config(process_config, folder_config)
            if process_config["stop_before_cleanup"]:
                logger.log(
                    "\nSteam processes will be stopped immediately before cleanup: "
                    + ", ".join(process_config["process_names"])
                )
            if process_config["restart_after_cleanup"]:
                logger.log(f"Steam will be restarted after cleanup: {restart_executable}")

        if registry_config["enabled"]:
            matcher, registry_targets = validate_registry_config(registry_config)
            logger.log(
                f"\nRegistry queries ({registry_config['match_type']}): "
                + ", ".join(repr(term) for term in matcher.terms)
            )
            for hive, start_path in registry_targets:
                registry_cleaner = RegistryCleaner(
                    logger,
                    registry_config["delete_matching_keys"],
                    registry_config["delete_matching_values"],
                )
                shown_path = start_path or "<hive root>"
                logger.log(f"\nSearching {HIVE_NAMES[hive]}\\{shown_path} ...")
                registry_cleaner.find(
                    hive,
                    start_path,
                    matcher,
                    registry_config["recursive"],
                )
                registry_cleaner.preview()
                registry_jobs.append((hive, start_path, registry_cleaner))
    except ConfigurationError as exc:
        logger.log(f"\nCONFIGURATION ERROR: {exc}")
        logger.log("No changes were made.")
        logger.save()
        return 2

    matching_registry_jobs = [job for job in registry_jobs if job[2].matches]
    has_registry_targets = bool(matching_registry_jobs)
    has_folder_targets = bool(folder_cleaner and folder_cleaner.to_delete)
    if not has_registry_targets and not has_folder_targets:
        logger.log("\nNothing matched. No changes were made.")
        logger.save()
        return 0

    if active_config["dry_run"]:
        logger.log(
            "\nDRY RUN is enabled. Nothing was deleted. Review the preview, then set "
            "dry_run to false in config.json only if it is correct. "
            "Steam processes were not stopped."
        )
        logger.save()
        return 0

    logger.log("\n" + "=" * 72)
    logger.log("The targets shown above will be PERMANENTLY DELETED.")
    if not confirm_execution(active_config, logger):
        logger.log("Cancelled. No changes were made.")
        logger.save()
        return 0

    logger.log("\n" + "=" * 72)
    logger.log("EXECUTING")
    logger.log("=" * 72)

    # Make every required registry backup before stopping Steam or deleting
    # anything. The deletion order remains folder first, registry second.
    registry_ready = True
    if has_registry_targets:
        if registry_config["backup_before_delete"]:
            for hive, start_path, _cleaner in matching_registry_jobs:
                if not backup_registry(
                    hive,
                    start_path,
                    registry_config["backup_dir"],
                    logger,
                ):
                    registry_ready = False
        if not registry_ready:
            logger.log(
                "ALL CLEANUP ABORTED because at least one required registry backup failed."
            )
            logger.save()
            return 3

    processes_were_running = False
    if process_config.get("enabled", False) and process_config["stop_before_cleanup"]:
        processes_were_running, processes_stopped = stop_steam_processes(
            process_config, logger
        )
        if not processes_stopped:
            logger.log("ALL CLEANUP ABORTED because Steam processes could not be stopped.")
            if process_config["restart_after_cleanup"] and restart_executable is not None:
                restart_steam(restart_executable, logger)
            logger.save()
            return 4

    folder_failed = 0
    registry_failed = 0
    restart_failed = False
    try:
        if has_folder_targets and folder_cleaner is not None:
            _folder_deleted, folder_failed = folder_cleaner.delete_all()

        if has_registry_targets:
            if folder_failed:
                logger.log(
                    "Registry deletion ABORTED because folder cleanup had errors. "
                    "Resolve them and run a new preview."
                )
            else:
                for hive, _start_path, registry_cleaner in matching_registry_jobs:
                    logger.log(f"\nDeleting registry targets in {HIVE_NAMES[hive]} ...")
                    _deleted, failed = registry_cleaner.delete_all(hive)
                    registry_failed += failed
    finally:
        if (
            process_config.get("enabled", False)
            and process_config["restart_after_cleanup"]
            and restart_executable is not None
        ):
            restart_failed = not restart_steam(restart_executable, logger)

    failures = folder_failed + registry_failed + int(restart_failed)
    if processes_were_running:
        logger.log("Steam had been running before cleanup.")
    if failures:
        logger.log(f"\nDone with {failures} error(s). Log: {logger.path.resolve()}")
    else:
        logger.log(f"\nDone successfully. Log: {logger.path.resolve()}")
    logger.save()
    return 1 if failures else 0


def check_configuration(config: dict) -> None:
    """Validate configuration and local paths without scanning or changing data."""
    validate_config_types(config)
    registry_config = config["registry"]
    folder_config = config["folder"]
    process_config = config.get("steam_processes", {"enabled": False})
    logger = Logger(os.devnull)

    if folder_config["enabled"]:
        cleaner = FolderCleaner(logger)
        cleaner.prepare(
            folder_config["path"],
            folder_config["keep_folder"],
            folder_config["keep_file"],
        )
    if registry_config["enabled"]:
        validate_registry_config(registry_config)
    if process_config.get("enabled", False):
        validate_process_config(process_config, folder_config)


def cli_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Preview and clean selected Steam files and registry records.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="path to config.json (default: next to the script/executable)",
    )
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--create-config",
        action="store_true",
        help="create a safe default configuration and exit",
    )
    action_group.add_argument(
        "--check-config",
        action="store_true",
        help="validate config and paths without scanning or changing anything",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    args = parser.parse_args(argv)

    try:
        if args.create_config:
            write_default_config(args.config)
            print(f"Configuration created: {args.config.resolve()}")
            return 0
        config = load_config_file(args.config)
        if args.check_config:
            check_configuration(config)
            print(f"Configuration is valid: {args.config.resolve()}")
            return 0
    except ConfigurationError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2
    return main(config)


if __name__ == "__main__":
    raise SystemExit(cli_main())
