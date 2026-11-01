from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install
import paper_launcher as launcher


def tar_bytes(entries: list[tuple[str, bytes | str, int]], *, compression: str = "xz") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode=f"w:{compression}") as archive:
        for name, content, mode in entries:
            member = tarfile.TarInfo(name)
            member.mode = mode
            if isinstance(content, str):
                member.type = tarfile.SYMTYPE
                member.linkname = content
                archive.addfile(member)
            else:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def deb_bytes(version: str = "0.5.16", *, payload: bytes | None = None) -> bytes:
    if payload is None:
        payload = tar_bytes(
            [
                ("./opt/Paper/paper-desktop", f"#!/bin/sh\necho {version}\n".encode(), 0o755),
                ("./opt/Paper/chrome-sandbox", b"helper", 0o4755),
                ("./opt/Paper/chrome_crashpad_handler", b"helper", 0o755),
                ("./opt/Paper/resources/app-update.yml", b"provider: generic\n", 0o644),
                ("./usr/share/icons/hicolor/256x256/apps/paper-desktop.png", b"icon", 0o644),
            ]
        )
    output = bytearray(b"!<arch>\n")
    control = tar_bytes([("./control", b"Package: paper\n", 0o644)])
    for name, content in (
        ("debian-binary", b"2.0\n"),
        ("control.tar.xz", control),
        ("data.tar.xz", payload),
    ):
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n"
        output.extend(header.encode())
        output.extend(content)
        if len(content) % 2:
            output.extend(b"\n")
    return bytes(output)


def metadata(content: bytes, version: str = "0.5.16", arch: str = "amd64") -> dict:
    return {
        "version": version,
        "files": [
            {
                "url": f"paper-desktop-{version}-build-fixture-{arch}.deb",
                "sha512": base64.b64encode(hashlib.sha512(content).digest()).decode(),
                "size": len(content),
            }
        ],
    }


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.installation = launcher.Installation(self.root)

    def update(self, state: dict, version: str = "0.5.16"):
        content = deb_bytes(version)
        release = launcher.parse_metadata(metadata(content, version), "amd64")
        with (
            patch.object(launcher, "release_metadata", return_value=release),
            patch.object(launcher, "open_url", side_effect=lambda *a, **kw: io.BytesIO(content)),
        ):
            self.installation.update(state, "amd64", force=True)
        return release

    def test_valid_metadata_for_both_architectures(self):
        for arch in ("amd64", "arm64"):
            release = launcher.parse_metadata(metadata(b"data", arch=arch), arch)
            self.assertEqual(release["arch"], arch)
            self.assertTrue(release["url"].startswith(launcher.RELEASE_BASE))

    def test_metadata_rejects_unsafe_urls_and_bad_hashes(self):
        for key, value in (
            ("url", "../escape-amd64.deb"),
            ("url", "https://evil.test/a-amd64.deb"),
            ("sha512", "bad!"),
            ("sha512", "YQ=="),
            ("size", -1),
            ("size", True),
        ):
            document = metadata(b"data")
            document["files"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(launcher.LauncherError):
                launcher.parse_metadata(document, "amd64")

    def test_metadata_accepts_different_filename_version_and_rejects_duplicates(self):
        document = metadata(b"data")
        document["version"] = "0.9.0"
        self.assertEqual(launcher.parse_metadata(document, "amd64")["version"], "0.9.0")
        document = metadata(b"data")
        document["files"] *= 2
        with self.assertRaises(launcher.LauncherError):
            launcher.parse_metadata(document, "amd64")

    def test_https_only(self):
        with self.assertRaises(launcher.LauncherError):
            launcher.open_url("http://example.test")
        with self.assertRaises(launcher.LauncherError):
            launcher.HTTPSRedirect().redirect_request(
                None, None, 302, "", {}, "http://example.test"
            )

    def test_download_rejects_modified_and_truncated_content(self):
        release = launcher.parse_metadata(metadata(b"good"), "amd64")
        for number, content in enumerate((b"evil", b"goo", b"good-extra")):
            with patch.object(
                launcher,
                "open_url",
                side_effect=lambda *a, content=content, **kw: io.BytesIO(content),
            ):
                with self.assertRaises(launcher.LauncherError):
                    launcher.download(release, self.root / f"bad-{number}.deb")

    def test_first_install_and_permissions(self):
        state = {}
        self.update(state)
        binary = self.installation.executable(state["current"])
        self.assertIsNotNone(binary)
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
        self.assertFalse(binary.with_name("chrome-sandbox").stat().st_mode & 0o4000)
        self.assertTrue(os.access(binary.with_name("chrome_crashpad_handler"), os.X_OK))
        self.assertTrue(binary.with_name("resources").joinpath("app-update.yml").is_file())
        self.assertEqual(self.installation.read_state(), state)
        self.assertEqual((self.root / "icon.png").read_bytes(), b"icon")

    def test_update_prunes_old_releases_but_not_user_files(self):
        state = {}
        first = self.update(state)
        second = self.update(state, "0.5.17")
        unowned = self.installation.releases / ("f" * 128)
        unowned.mkdir()
        (unowned / "keep").write_text("user data")
        third = self.update(state, "0.5.18")
        self.assertEqual(state["current"]["id"], third["sha512"])
        self.assertFalse((self.installation.releases / first["sha512"]).exists())
        self.assertFalse((self.installation.releases / second["sha512"]).exists())
        self.assertTrue((unowned / "keep").exists())

    def test_failed_update_keeps_working_state_and_cleans_staging(self):
        state = {}
        self.update(state)
        current = dict(state["current"])
        bad = launcher.parse_metadata(metadata(b"bad"), "amd64")
        with (
            patch.object(launcher, "release_metadata", return_value=bad),
            patch.object(launcher, "open_url", return_value=io.BytesIO(b"BAD")),
            self.assertRaises(launcher.LauncherError),
        ):
            self.installation.update(state, "amd64", force=True)
        self.assertEqual(self.installation.read_state()["current"], current)
        self.assertTrue(self.installation.executable(current))
        self.assertFalse(list(self.installation.releases.glob(".staging-*")))

    def test_atomic_commit_failure_keeps_current_pointer(self):
        state = {}
        self.update(state)
        before = self.installation.read_state()
        replacement = dict(state, last_check=0)
        with patch.object(launcher.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.installation.save_state(replacement)
        self.assertEqual(self.installation.read_state(), before)

    def test_daily_check_and_immediate_retry_after_failure(self):
        state = {}
        self.update(state)
        with patch.object(launcher, "release_metadata") as upstream:
            self.installation.update(state, "amd64")
            upstream.assert_not_called()
        state["last_check"] = 0
        with patch.object(launcher, "release_metadata", side_effect=OSError("offline")) as upstream:
            with self.assertRaises(OSError):
                self.installation.update(state, "amd64")
            with self.assertRaises(OSError):
                self.installation.update(state, "amd64")
            self.assertEqual(upstream.call_count, 2)

    def test_legacy_state_does_not_pause_updates(self):
        state = {}
        self.update(state)
        legacy = dict(
            state, previous=state["current"], updates_paused=True, last_check=0, last_attempt=0
        )
        self.installation.state_path.write_text(json.dumps(legacy))
        migrated = self.installation.read_state()
        self.assertNotIn("previous", migrated)
        self.assertNotIn("updates_paused", migrated)
        release = launcher.parse_metadata(metadata(deb_bytes()), "amd64")
        with patch.object(launcher, "release_metadata", return_value=release) as upstream:
            self.installation.update(migrated, "amd64")
            upstream.assert_called_once()
        self.assertEqual(self.installation.read_state(), migrated)

    def test_removed_command_is_rejected(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
            launcher.parser().parse_args(["--rollback"])
        self.assertEqual(error.exception.code, 2)
        self.assertNotIn("--rollback", launcher.parser().format_help())

    def test_tar_rejects_traversal_links_and_special_files(self):
        for index, (name, content, mode) in enumerate(
            (
                ("../../escaped", b"danger", 0o644),
                ("/tmp/escaped", b"danger", 0o644),
                ("opt/Paper/link", "../../outside", 0o777),
            )
        ):
            payload = self.root / f"payload-{index}.tar.xz"
            payload.write_bytes(
                tar_bytes(
                    [
                        ("opt/Paper/paper-desktop", b"binary", 0o755),
                        (name, content, mode),
                    ]
                )
            )
            output = self.root / f"extracted-{index}"
            output.mkdir()
            with self.assertRaises((launcher.LauncherError, tarfile.TarError)):
                launcher.extract_payload(payload, output)

    def test_tar_allows_safe_relative_symlink(self):
        payload = self.root / "safe.tar.xz"
        payload.write_bytes(
            tar_bytes(
                [
                    ("opt/Paper/paper-desktop", b"binary", 0o755),
                    ("opt/Paper/link", "paper-desktop", 0o777),
                ]
            )
        )
        output = self.root / "safe"
        output.mkdir()
        launcher.extract_payload(payload, output)
        self.assertEqual((output / "opt/Paper/link").read_bytes(), b"binary")

    def test_corrupt_and_truncated_deb_are_rejected(self):
        for index, content in enumerate((b"not a deb", deb_bytes()[:-100])):
            deb = self.root / f"broken-{index}.deb"
            deb.write_bytes(content)
            with self.assertRaises(launcher.LauncherError):
                launcher.unpack_deb(deb, self.root / f"output-{index}")

    def test_tar_rejects_special_files_and_extraction_limits(self):
        payload = self.root / "special.tar.xz"
        with tarfile.open(payload, "w:xz") as archive:
            member = tarfile.TarInfo("opt/Paper/fifo")
            member.type = tarfile.FIFOTYPE
            archive.addfile(member)
        with self.assertRaises(tarfile.TarError):
            launcher.extract_payload(payload, self.root)
        payload.write_bytes(tar_bytes([("opt/Paper/paper-desktop", b"too large", 0o755)]))
        with patch.object(launcher, "MAX_EXTRACTED", 1), self.assertRaises(launcher.LauncherError):
            launcher.extract_payload(payload, self.root)

    def test_invalid_state_is_not_overwritten(self):
        for state in (
            [],
            {"current": {"id": "../../outside", "version": "1"}},
            {"last_check": "tomorrow"},
            {"last_check": float("inf")},
        ):
            self.installation.state_path.write_text(json.dumps(state))
            with self.assertRaises(launcher.LauncherError):
                self.installation.read_state()

    def test_invalid_state_recovers_unique_install_and_preserves_original(self):
        state = {}
        release = self.update(state)
        self.installation.state_path.write_bytes(b"invalid state")
        recovered = self.installation.read_state()
        self.assertEqual(recovered["current"]["id"], release["sha512"])
        self.assertEqual(recovered["current"]["version"], "unknown")
        with patch.object(launcher, "release_metadata", return_value=release):
            self.installation.update(recovered, "amd64")
        self.assertEqual(recovered["current"]["version"], release["version"])
        backups = list(self.root.glob("state.invalid-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"invalid state")

    def test_invalid_state_with_multiple_releases_is_not_recovered(self):
        state = {}
        first = self.update(state)
        with patch.object(self.installation, "prune"):
            second = self.update(state, "0.5.17")
        self.assertNotEqual(first["sha512"], second["sha512"])
        self.installation.state_path.write_bytes(b"invalid state")
        with self.assertRaises(launcher.LauncherError):
            self.installation.read_state()
        self.assertEqual(self.installation.state_path.read_bytes(), b"invalid state")

    def test_cli_forwards_protocol_url_without_shell_interpolation(self):
        state = {}
        root = self.root / "paper-launcher"
        root.mkdir()
        self.installation = launcher.Installation(root)
        self.update(state)
        url = "paper://auth?token=fixture&name=a%20b"
        with (
            patch.dict(
                os.environ,
                {
                    "XDG_DATA_HOME": str(self.root),
                    "APPIMAGE": "/fixture/parent.AppImage",
                    "APPDIR": "/fixture/parent",
                },
            ),
            patch.object(launcher.os, "chdir"),
            patch.object(launcher.os, "execve", side_effect=RuntimeError("exec")) as execute,
        ):
            with self.assertRaisesRegex(RuntimeError, "exec"):
                launcher.main(["--skip-update", "--", url])
            self.assertEqual(execute.call_args.args[1][1:], [url])
            self.assertNotIn("--no-sandbox", execute.call_args.args[1])
            self.assertNotIn("APPIMAGE", execute.call_args.args[2])
            self.assertNotIn("APPDIR", execute.call_args.args[2])

    def test_cli_offline_fallback(self):
        state = {}
        root = self.root / "paper-launcher"
        root.mkdir()
        self.installation = launcher.Installation(root)
        self.update(state)
        state.update(last_check=0, last_attempt=0)
        self.installation.save_state(state)
        with (
            patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root)}),
            patch.object(launcher, "release_metadata", side_effect=OSError("offline")),
            patch.object(launcher, "show_error"),
            patch.object(launcher.os, "chdir"),
            patch.object(launcher.os, "execve", side_effect=RuntimeError("exec")) as execute,
        ):
            with self.assertRaisesRegex(RuntimeError, "exec"):
                launcher.main([])
            execute.assert_called_once()
            with self.assertRaises(launcher.LauncherError):
                launcher.main(["--update"])

    def test_resume_download_validates_range_and_checksum(self):
        content = b"complete package"
        release = launcher.parse_metadata(metadata(content), "amd64")

        class Partial(io.BytesIO):
            status = 206
            headers = {"Content-Range": f"bytes 4-{len(content) - 1}/{len(content)}"}

        with patch.object(
            launcher, "open_url", side_effect=[io.BytesIO(content[:4]), Partial(content[4:])]
        ) as fetch:
            launcher.download(release, self.root / "resumed.deb")
        self.assertEqual((self.root / "resumed.deb").read_bytes(), content)
        self.assertEqual(fetch.call_args.kwargs["offset"], 4)

    def test_server_ignoring_range_restarts_download(self):
        content = b"complete package"
        release = launcher.parse_metadata(metadata(content), "amd64")
        with patch.object(
            launcher, "open_url", side_effect=[io.BytesIO(content[:4]), io.BytesIO(content)]
        ):
            launcher.download(release, self.root / "restarted.deb")
        self.assertEqual((self.root / "restarted.deb").read_bytes(), content)

    def test_config_and_dry_run_preserve_arguments(self):
        root = self.root / "paper-launcher"
        root.mkdir()
        self.installation = launcher.Installation(root)
        self.update({})
        config = self.root / "config"
        config.mkdir()
        (config / "paper-launcher.conf").write_text(
            '[paper]\nextra_arguments = ["--ozone-platform=wayland"]\n'
            'extra_env_vars = ["TEST_PAPER=value"]\ndownload_attempts = 3\n'
        )
        output = io.StringIO()
        with (
            patch.dict(
                os.environ, {"XDG_DATA_HOME": str(self.root), "XDG_CONFIG_HOME": str(config)}
            ),
            patch("sys.stdout", output),
            patch.object(launcher.subprocess, "call") as execute,
        ):
            self.assertEqual(launcher.main(["--skip-update", "--no-exec", "--", "paper://test"]), 0)
            execute.assert_not_called()
        self.assertIn("--ozone-platform=wayland", output.getvalue())
        self.assertIn("paper://test", output.getvalue())

    def test_invalid_config_rejected(self):
        config = self.root / "config"
        config.mkdir()
        (config / "paper-launcher.conf").write_text('[paper]\nextra_env_vars = ["INVALID"]\n')
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}),
            self.assertRaises(launcher.LauncherError),
        ):
            launcher.load_config()

    def test_progress_failure_does_not_break_download(self):
        content = b"package"
        release = launcher.parse_metadata(metadata(content), "amd64")
        with (
            patch.dict(os.environ, {"DISPLAY": ":99"}),
            patch.object(launcher.shutil, "which", return_value="/usr/bin/zenity"),
            patch.object(launcher.subprocess, "Popen", side_effect=OSError("missing display")),
            patch.object(launcher, "open_url", return_value=io.BytesIO(content)),
        ):
            launcher.download(release, self.root / "without-dialog.deb")

    def test_running_app_blocks_updates_but_allows_launch(self):
        state = {}
        root = self.root / "paper-launcher"
        root.mkdir()
        self.installation = launcher.Installation(root)
        self.update(state)
        # A separate process holds the shared lifetime lock, just like a running launcher.
        script = "import fcntl,sys; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_SH); print('locked',flush=True); sys.stdin.read()"
        with subprocess.Popen(
            ["python3", "-c", script, str(root / "launcher.lock")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as process:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            try:
                with (
                    patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root)}),
                    patch.object(launcher.os, "chdir"),
                    patch.object(
                        launcher.os, "execve", side_effect=RuntimeError("exec")
                    ) as execute,
                    patch.object(launcher, "release_metadata") as upstream,
                ):
                    with self.assertRaises(launcher.LauncherError):
                        launcher.main(["--update"])
                    with self.assertRaisesRegex(RuntimeError, "exec"):
                        launcher.main([])
                    execute.assert_called_once()
                    upstream.assert_not_called()
            finally:
                process.communicate(timeout=5)

    def test_exec_keeps_lock_until_application_exits(self):
        root = self.root / "paper-launcher"
        root.mkdir()
        self.installation = launcher.Installation(root)
        state = {}
        self.update(state)
        executable = self.installation.executable(state["current"])
        assert executable is not None
        executable.write_text("#!/bin/sh\necho ready\nread line\n")
        script = (
            "import paper_launcher as p; "
            "p.shutil.which=lambda _: '/usr/bin/zenity'; "
            "p.os.geteuid=lambda: 1000; p.main(['--skip-update'])"
        )
        environment = os.environ.copy()
        environment.update(XDG_DATA_HOME=str(self.root), XDG_CONFIG_HOME=str(self.root))
        with subprocess.Popen(
            ["python3", "-c", script],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                with (root / "launcher.lock").open("a+") as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                process.communicate("done\n", timeout=5)
                self.assertEqual(process.returncode, 0)
                with (root / "launcher.lock").open("a+") as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class InstallerTests(unittest.TestCase):
    def test_installer_requires_zenity_before_writing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(Path, "home", return_value=root),
                patch.object(install.shutil, "which", return_value=None),
                self.assertRaisesRegex(ValueError, "Zenity is required"),
            ):
                install.install()
            self.assertEqual(list(root.iterdir()), [])

    def test_install_is_idempotent_and_does_not_overwrite_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(Path, "home", return_value=root),
                patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data")}),
                patch.object(
                    install.shutil,
                    "which",
                    side_effect=lambda name: "/usr/bin/zenity" if name == "zenity" else None,
                ),
            ):
                install.install()
                install.install()
                binary = root / ".local/bin/paper-launcher"
                self.assertTrue(os.access(binary, os.X_OK))
                desktop = root / "data/applications/paper-launcher.desktop"
                self.assertIn(" -- %U", desktop.read_text())
                binary.write_text("user-owned file")
                with self.assertRaises(ValueError):
                    install.install()
                self.assertEqual(binary.read_text(), "user-owned file")


if __name__ == "__main__":
    unittest.main()
