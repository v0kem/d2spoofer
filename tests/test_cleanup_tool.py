import builtins
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cleanup_tool as tool


class MatcherTests(unittest.TestCase):
    def test_independent_partial_queries_and_exclusion(self):
        matcher = tool.Matcher(
            ["dota", "steam", "valve"], "partial", ["msteams"]
        )
        self.assertEqual(matcher.matching_terms("Steam and Valve"), ["steam", "valve"])
        self.assertFalse(matcher.matches("MSTeams_8wekyb3d8bbwe"))

    def test_binary_log_preview_is_compact(self):
        data = b"\x00\xfeSteam" + b"\x00" * 1000
        self.assertEqual(tool.format_registry_data(data), "<binary data: 1007 bytes>")


class ConfigurationTests(unittest.TestCase):
    def test_external_config_merges_defaults_and_resolves_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            path.write_text(
                json.dumps({
                    "dry_run": True,
                    "log_file": "logs/run.log",
                    "registry": {"backup_dir": "backups"},
                }),
                encoding="utf-8",
            )
            config = tool.load_config_file(path)
            self.assertEqual(Path(config["log_file"]), root / "logs" / "run.log")
            self.assertEqual(Path(config["registry"]["backup_dir"]), root / "backups")
            self.assertEqual(config["registry"]["targets"][0]["hive"], "HKCR")

    def test_unknown_config_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text('{"dry_rnu": true}', encoding="utf-8")
            with self.assertRaises(tool.ConfigurationError):
                tool.load_config_file(path)

    def test_dry_run_requires_a_real_boolean(self):
        config = copy.deepcopy(tool.CONFIG)
        config["dry_run"] = 0
        with self.assertRaises(tool.ConfigurationError):
            tool.validate_config_types(config)

    def test_all_registry_hives_validate(self):
        _matcher, targets = tool.validate_registry_config(tool.CONFIG["registry"])
        self.assertEqual(len(targets), 5)
        self.assertTrue(all(path == "" for _hive, path in targets))

    def test_check_configuration_uses_no_registry_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "steamapps").mkdir()
            (root / "steam.exe").write_bytes(b"test")
            config = copy.deepcopy(tool.CONFIG)
            config["folder"]["path"] = str(root)
            config["registry"]["enabled"] = False
            tool.check_configuration(config)


class SafetyTests(unittest.TestCase):
    def test_critical_folder_rules(self):
        self.assertTrue(tool.folder_is_dangerous(Path(r"C:\Program Files (x86)")))
        self.assertFalse(
            tool.folder_is_dangerous(Path(r"C:\Program Files (x86)\Steam"))
        )
        self.assertTrue(tool.folder_is_dangerous(Path(r"C:\Windows\System32")))

    def test_dry_run_never_deletes_or_prompts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "keep.txt").write_text("keep", encoding="utf-8")
            remove = root / "remove.txt"
            remove.write_text("remove", encoding="utf-8")
            config = {
                "dry_run": True,
                "log_file": str(root / "run.log"),
                "registry": {"enabled": False},
                "folder": {
                    "enabled": True,
                    "path": str(root),
                    "keep_folder": None,
                    "keep_file": "keep.txt",
                },
                "steam_processes": {"enabled": False},
            }
            with mock.patch.object(
                builtins, "input", side_effect=AssertionError("dry run prompted")
            ):
                self.assertEqual(tool.main(config), 0)
            self.assertTrue(remove.exists())

    def test_countdown_confirmation_continues_automatically(self):
        config = {"confirmation": {"mode": "countdown", "countdown_seconds": 3}}
        with tempfile.TemporaryDirectory() as temporary:
            logger = tool.Logger(str(Path(temporary) / "countdown.log"))
            with mock.patch.object(tool.time, "sleep", return_value=None) as sleeper:
                self.assertTrue(tool.confirm_execution(config, logger))
        self.assertEqual(sleeper.call_count, 3)

    def test_countdown_ctrl_c_cancels(self):
        config = {"confirmation": {"mode": "countdown", "countdown_seconds": 15}}
        with tempfile.TemporaryDirectory() as temporary:
            logger = tool.Logger(str(Path(temporary) / "countdown.log"))
            with mock.patch.object(tool.time, "sleep", side_effect=KeyboardInterrupt):
                self.assertFalse(tool.confirm_execution(config, logger))


class ProcessTests(unittest.TestCase):
    def test_process_stop_flow(self):
        config = copy.deepcopy(tool.CONFIG["steam_processes"])
        state = {name.casefold(): False for name in config["process_names"]}
        state["steam.exe"] = True

        def running(name):
            return state[name.casefold()]

        def taskkill(name, force):
            state[name.casefold()] = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temporary:
            logger = tool.Logger(str(Path(temporary) / "process.log"))
            with mock.patch.object(tool, "process_is_running", side_effect=running), mock.patch.object(
                tool, "_taskkill", side_effect=taskkill
            ), mock.patch.object(tool.time, "sleep", return_value=None):
                was_running, success = tool.stop_steam_processes(config, logger)
        self.assertTrue(was_running)
        self.assertTrue(success)


if __name__ == "__main__":
    unittest.main()
