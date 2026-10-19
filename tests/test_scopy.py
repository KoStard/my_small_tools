"""Offline SSH transfer checks: uv run python -m unittest discover -s tests -p test_scopy.py."""

import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from my_small_tools import scopy


class ScopyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote home ' with spaces"
        self.remote.mkdir()
        self.local_home = self.root / "local-home"
        self.local_home.mkdir()
        self.config = self.root / "config" / "scopy-target"
        self.runner = CliRunner()
        self.real_run = subprocess.run
        self.real_popen = subprocess.Popen

    def tearDown(self):
        self.temporary.cleanup()

    def remote_command(self, arguments):
        self.assertEqual(arguments[0], "ssh")
        self.assertEqual(arguments[-2], "test-host")
        return ["/bin/sh", "-c", arguments[-1]]

    def run_ssh(self, arguments, **kwargs):
        # subprocess.run itself uses Popen, which is patched below.
        return self.real_run(arguments, **kwargs)

    def popen_ssh(self, arguments, **kwargs):
        return self.real_popen(self.remote_command(arguments),
                               env={**os.environ, "HOME": str(self.remote)}, **kwargs)

    def invoke(self, arguments):
        with patch.object(scopy, "config_path", return_value=self.config), \
                patch.object(scopy.Path, "home", return_value=self.local_home), \
                patch.object(scopy.subprocess, "run", side_effect=self.run_ssh), \
                patch.object(scopy.subprocess, "Popen", side_effect=self.popen_ssh):
            return self.runner.invoke(scopy.main, arguments)

    def configure(self):
        result = self.invoke(["set-target", "test-host"])
        self.assertEqual(result.exit_code, 0, result.output)

    def invoke_tunnel(self, ports, process):
        with patch.object(scopy, "config_path", return_value=self.config), \
                patch.object(scopy.shutil, "which", return_value="/usr/bin/ssh"), \
                patch.object(scopy.subprocess, "Popen") as popen:
            popen.return_value.__enter__.return_value = process
            result = self.runner.invoke(scopy.main, ["tunnel", *ports])
            return result, popen

    def test_tunnel_uses_saved_target_and_loopback_ports(self):
        self.configure()
        for ports, expected in [(["8000"], "127.0.0.1:8000:127.0.0.1:8000"),
                                (["8080", "8000"], "127.0.0.1:8080:127.0.0.1:8000")]:
            with self.subTest(ports=ports):
                process = MagicMock()
                process.wait.return_value = 0
                result, popen = self.invoke_tunnel(ports, process)
                self.assertEqual(result.exit_code, 0, result.output)
                command = popen.call_args.args[0]
                self.assertEqual(command[-1], "test-host")
                self.assertEqual(command[command.index("-L") + 1], expected)
                self.assertIn("-N", command)
                self.assertIn("ExitOnForwardFailure=yes", command)
                self.assertIn("ControlPath=none", command)
                self.assertIn("ForkAfterAuthentication=no", command)
                self.assertIn("test-host:127.0.0.1:8000", result.output)
                self.assertIn("Ctrl+C", result.output)

    def test_tunnel_rejects_missing_or_invalid_ports(self):
        for ports in [[], ["0"], ["65536"], ["abc"], ["8000", "0"], ["8000", "65536"]]:
            with self.subTest(ports=ports):
                result, popen = self.invoke_tunnel(ports, MagicMock())
                self.assertEqual(result.exit_code, 2, result.output)
                popen.assert_not_called()

    def test_tunnel_requires_configured_target(self):
        result, popen = self.invoke_tunnel(["8000"], MagicMock())
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("set-target", result.output)
        popen.assert_not_called()

    def test_tunnel_reports_ssh_failure(self):
        self.configure()
        process = MagicMock()
        process.wait.return_value = 255
        result, _ = self.invoke_tunnel(["8000"], process)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("SSH tunnel failed (exit status 255)", result.output)

    def test_tunnel_ctrl_c_terminates_connection(self):
        self.configure()
        process = MagicMock()
        process.wait.side_effect = [KeyboardInterrupt, 0]
        process.poll.return_value = None
        result, _ = self.invoke_tunnel(["8000"], process)
        self.assertEqual(result.exit_code, 0, result.output)
        process.terminate.assert_called_once()
        process.wait.assert_any_call(timeout=5)
        process.kill.assert_not_called()
        self.assertIn("Tunnel closed", result.output)

    def test_tunnel_ctrl_c_kills_unresponsive_connection(self):
        self.configure()
        process = MagicMock()
        process.wait.side_effect = [KeyboardInterrupt, subprocess.TimeoutExpired("ssh", 5), 0]
        process.poll.return_value = None
        result, _ = self.invoke_tunnel(["8000"], process)
        self.assertEqual(result.exit_code, 0, result.output)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_tunnel_reports_missing_ssh_and_process_start_failure(self):
        self.configure()
        with patch.object(scopy, "config_path", return_value=self.config), \
                patch.object(scopy.shutil, "which", return_value=None):
            result = self.runner.invoke(scopy.main, ["tunnel", "8000"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ssh is required", result.output)
        with patch.object(scopy, "config_path", return_value=self.config), \
                patch.object(scopy.shutil, "which", return_value="/usr/bin/ssh"), \
                patch.object(scopy.subprocess, "Popen", side_effect=OSError("cannot execute")):
            result = self.runner.invoke(scopy.main, ["tunnel", "8000"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Cannot start SSH tunnel", result.output)

    def test_missing_target_and_invalid_target(self):
        source = self.root / "file"
        source.touch()
        result = self.invoke(["send", str(source)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("set-target", result.output)
        result = self.invoke(["set-target", "--", "-oProxyCommand=bad"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(self.config.exists())

    def test_file_collisions_and_shell_characters(self):
        self.configure()
        source = self.root / "report ' $(touch injected).txt"
        source.write_bytes(bytes(range(256)))
        for index in range(3):
            result = self.invoke(["send", str(source)])
            self.assertEqual(result.exit_code, 0, result.output)
            suffix = "" if index == 0 else f"_{index}"
            destination = self.remote / "Downloads" / f"{source.stem}{suffix}.txt"
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertIn("Destination:", result.output)
            self.assertIn("Copied", result.output)
        self.assertFalse((self.root / "injected").exists())

    def test_folder_contents_collisions_and_symlinks(self):
        self.configure()
        source = self.root / "photos.v1"
        source.mkdir()
        (source / ".hidden").write_text("hidden")
        (source / "empty").mkdir()
        (source / "link").symlink_to(".hidden")
        for name in [source.name, source.name + "_1"]:
            result = self.invoke(["send", str(source)])
            self.assertEqual(result.exit_code, 0, result.output)
            destination = self.remote / "Downloads" / name
            self.assertEqual((destination / ".hidden").read_text(), "hidden")
            self.assertTrue((destination / "empty").is_dir())
            self.assertEqual((destination / "link").readlink(), Path(".hidden"))

    def test_extensionless_file_skips_dangling_symlink(self):
        self.configure()
        source = self.root / "LICENSE"
        source.write_text("license")
        downloads = self.remote / "Downloads"
        downloads.mkdir()
        (downloads / "LICENSE").symlink_to("missing")
        result = self.invoke(["send", str(source)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual((downloads / "LICENSE_1").read_text(), "license")
        self.assertTrue((downloads / "LICENSE").is_symlink())

    def test_simultaneous_sends_reserve_different_names(self):
        source = self.root / "same.txt"
        source.write_text("payload")
        with patch.object(scopy.subprocess, "Popen", side_effect=self.popen_ssh):
            with ThreadPoolExecutor(max_workers=4) as executor:
                destinations = list(executor.map(
                    lambda _: scopy.copy_to_target(source, "test-host"), range(4)))
        self.assertEqual(len(set(destinations)), 4)
        for destination in destinations:
            self.assertEqual(Path(destination).read_text(), "payload")

    def test_failed_transfer_reports_reserved_path(self):
        self.configure()
        source = self.root / "file.txt"
        source.write_text("payload")
        original_popen = self.popen_ssh

        def fail_transfer(arguments, **kwargs):
            if arguments[-1].startswith("cat >"):
                return self.real_popen(["/bin/sh", "-c", "exit 1"], **kwargs)
            return original_popen(arguments, **kwargs)

        with patch.object(self, "popen_ssh", side_effect=fail_transfer):
            result = self.invoke(["send", str(source)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("incomplete copy", result.output)
        self.assertIn("file.txt", result.output)

    def test_download_file_with_remote_home_and_collisions(self):
        self.configure()
        source = self.remote / "report ' $(touch injected).txt"
        source.write_bytes(bytes(range(256)) * 9000)
        for suffix in ["", "_1"]:
            result = self.invoke(["download", "~/" + source.name])
            self.assertEqual(result.exit_code, 0, result.output)
            destination = self.local_home / "Downloads" / f"{source.stem}{suffix}.txt"
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertIn("Source:", result.output)
            self.assertIn("Destination:", result.output)
            self.assertIn("Downloaded", result.output)

    def test_download_folder_with_relative_path_and_contents(self):
        self.configure()
        source = self.remote / "photos.v1"
        source.mkdir()
        (source / ".hidden").write_text("hidden")
        (source / "large.bin").write_bytes(b"x" * (scopy.BUFFER_SIZE * 2 + 100))
        (source / "empty").mkdir()
        (source / "link").symlink_to(".hidden")
        for suffix in ["", "_1"]:
            result = self.invoke(["download", "photos.v1/"])
            self.assertEqual(result.exit_code, 0, result.output)
            destination = self.local_home / "Downloads" / f"photos.v1{suffix}"
            self.assertEqual((destination / ".hidden").read_text(), "hidden")
            self.assertEqual((destination / "large.bin").read_bytes(), (source / "large.bin").read_bytes())
            self.assertTrue((destination / "empty").is_dir())
            self.assertEqual((destination / "link").readlink(), Path(".hidden"))

    def test_download_missing_path_does_not_create_destination(self):
        self.configure()
        result = self.invoke(["download", "/missing-scopy-fixture"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse((self.local_home / "Downloads").exists())

    def test_download_rejects_folder_link_outside_destination(self):
        self.configure()
        source = self.remote / "unsafe"
        source.mkdir()
        (source / "link").symlink_to("../outside")
        result = self.invoke(["download", "unsafe"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Download failed", result.output)
        self.assertFalse((self.local_home / "Downloads" / "unsafe" / "link").exists())

    def test_download_failed_stream_reports_partial_file(self):
        self.configure()
        (self.remote / "file.txt").write_text("full payload")
        original_popen = self.popen_ssh

        def fail_download(arguments, **kwargs):
            if scopy.RANGE_SCRIPT in arguments[-1]:
                return self.real_popen(["/bin/sh", "-c", "printf partial; exit 1"], **kwargs)
            return original_popen(arguments, **kwargs)

        with patch.object(self, "popen_ssh", side_effect=fail_download):
            result = self.invoke(["download", "file.txt"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("incomplete copy", result.output)
        partial = (self.local_home / "Downloads" / "file.txt").read_bytes()
        self.assertTrue(partial.startswith(b"partial"))

    def test_empty_download_and_ssh_performance_options(self):
        self.configure()
        (self.remote / "empty").touch()
        result = self.invoke(["download", "empty"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual((self.local_home / "Downloads" / "empty").stat().st_size, 0)
        arguments = scopy.ssh_command("test-host", "true")
        self.assertIn("Compression=no", arguments)
        if os.name == "posix":
            self.assertIn("ControlMaster=auto", arguments)
            self.assertIn("ControlPersist=60", arguments)

    def test_multiple_sends_mix_files_folders_and_repeated_paths(self):
        self.configure()
        file = self.root / "file.txt"
        file.write_text("payload")
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "nested.txt").write_text("nested")
        result = self.invoke(["send", str(file), str(folder), str(file)])
        self.assertEqual(result.exit_code, 0, result.output)
        downloads = self.remote / "Downloads"
        self.assertEqual((downloads / "file.txt").read_text(), "payload")
        self.assertEqual((downloads / "file_1.txt").read_text(), "payload")
        self.assertEqual((downloads / "folder" / "nested.txt").read_text(), "nested")
        self.assertIn("[3/3] Sending:", result.output)

    def test_multiple_downloads_mix_files_folders_and_repeated_paths(self):
        self.configure()
        (self.remote / "file.txt").write_text("payload")
        (self.remote / "folder").mkdir()
        (self.remote / "folder" / "nested.txt").write_text("nested")
        result = self.invoke(["download", "file.txt", "~/folder", "file.txt"])
        self.assertEqual(result.exit_code, 0, result.output)
        downloads = self.local_home / "Downloads"
        self.assertEqual((downloads / "file.txt").read_text(), "payload")
        self.assertEqual((downloads / "file_1.txt").read_text(), "payload")
        self.assertEqual((downloads / "folder" / "nested.txt").read_text(), "nested")
        self.assertIn("[3/3] Downloading:", result.output)

    def test_multi_send_continues_after_missing_source(self):
        self.configure()
        file = self.root / "file.txt"
        file.write_text("payload")
        result = self.invoke(["send", str(self.root / "missing"), str(file)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("1 transfer(s) failed", result.output)
        self.assertIn("missing", result.output)
        self.assertEqual((self.remote / "Downloads" / "file.txt").read_text(), "payload")

    def test_multi_download_continues_and_summarizes_all_failures(self):
        self.configure()
        (self.remote / "file.txt").write_text("payload")
        result = self.invoke(["download", "missing-one", "file.txt", "missing-two"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("2 transfer(s) failed", result.output)
        self.assertIn("missing-one", result.output)
        self.assertIn("missing-two", result.output)
        self.assertEqual((self.local_home / "Downloads" / "file.txt").read_text(), "payload")

    def test_send_and_download_require_at_least_one_path(self):
        for command in ["send", "download"]:
            result = self.invoke([command])
            self.assertEqual(result.exit_code, 2)
            self.assertIn("Missing argument", result.output)


if __name__ == "__main__":
    unittest.main()
