#!/usr/bin/env python3
"""User-local Paper installer and launch-time updater. Linux, Python 3.12+."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import http.client
import json
import logging
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, TypedDict, cast

import yaml

# Taken from resources/app-update.yml in Paper's official Linux DEB.
RELEASE_BASE = "https://download.todesktop.com/2601167vjw8xe/"
CHECK_INTERVAL = 24 * 60 * 60
MAX_DOWNLOAD = 1024 * 1024 * 1024
MAX_EXTRACTED = 2 * MAX_DOWNLOAD
VERSION = "0.1.0"
log = logging.getLogger("paper-launcher")


class Release(TypedDict):
    version: str
    url: str
    size: int
    sha512: str
    arch: str


class InstalledRelease(TypedDict):
    id: str
    version: str


class State(TypedDict, total=False):
    current: InstalledRelease | None
    last_check: float
    last_attempt: float


class LauncherError(Exception):
    """An actionable error safe to display to the user."""


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise LauncherError("Refusing a non-HTTPS download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url: str, timeout: int = 15, *, offset: int = 0):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise LauncherError("Downloads must use HTTPS")
    opener = urllib.request.build_opener(HTTPSRedirect())
    headers = {"User-Agent": f"paper-launcher/{VERSION}"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
        headers["Accept-Encoding"] = "identity"
    request = urllib.request.Request(url, headers=headers)
    return opener.open(request, timeout=timeout)


def architecture() -> str:
    machine = platform.machine()
    try:
        return {"x86_64": "amd64", "aarch64": "arm64"}[machine]
    except KeyError as exc:
        raise LauncherError(f"Unsupported architecture: {machine}") from exc


def release_metadata(arch: str) -> Release:
    name = "latest-linux.yml" if arch == "amd64" else "latest-linux-arm64.yml"
    with open_url(RELEASE_BASE + name) as response:
        content = response.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise LauncherError("Update metadata is unexpectedly large")
    try:
        return parse_metadata(yaml.safe_load(content), arch)
    except yaml.YAMLError as exc:
        raise LauncherError("Invalid update metadata") from exc


def parse_metadata(document: Any, arch: str) -> Release:
    if not isinstance(document, dict):
        raise LauncherError("Invalid update metadata")
    version = document.get("version")
    files = document.get("files")
    if not isinstance(version, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9.+_~:-]{0,127}", version
    ):
        raise LauncherError("Invalid release version")
    if not isinstance(files, list):
        raise LauncherError("Missing release files")
    matches: list[Release] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        name = entry.get("url")
        if not isinstance(name, str) or not name.endswith(f"-{arch}.deb"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_~-]*\.deb", name):
            raise LauncherError("Unsafe DEB URL in update metadata")
        size = entry.get("size")
        digest = entry.get("sha512")
        if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
            raise LauncherError("Invalid DEB size")
        try:
            decoded = base64.b64decode(digest, validate=True) if isinstance(digest, str) else b""
        except (ValueError, binascii.Error) as exc:
            raise LauncherError("Invalid DEB checksum") from exc
        if len(decoded) != 64:
            raise LauncherError("Invalid DEB checksum")
        matches.append(
            {
                "version": version,
                "url": RELEASE_BASE + name,
                "size": size,
                "sha512": decoded.hex(),
                "arch": arch,
            }
        )
    if len(matches) != 1:
        raise LauncherError(f"Expected exactly one {arch} DEB in the update feed")
    return matches[0]


class Progress:
    """Best-effort graphical progress; failure never affects the update."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which(
            "zenity"
        ):
            try:
                self.process = subprocess.Popen(
                    [
                        "zenity",
                        "--progress",
                        "--title=Downloading Paper",
                        "--text=Downloading…",
                        "--no-cancel",
                        "--auto-close",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except OSError:
                log.debug("Could not start Zenity", exc_info=True)

    def update(self, percent: int) -> None:
        if self.process and self.process.stdin:
            try:
                self.process.stdin.write(f"{percent}\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                log.debug("Zenity progress unavailable", exc_info=True)

    def close(self) -> None:
        if self.process:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                if self.process.poll() is None:
                    self.process.terminate()
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except OSError:
                pass


def download(release: Release, destination: Path, *, attempts: int = 5, timeout: int = 30) -> None:
    digest = hashlib.sha512()
    size = 0
    progress = Progress()
    try:
        with destination.open("xb") as output:
            for attempt in range(attempts):
                try:
                    kwargs = {"timeout": timeout}
                    if size:
                        kwargs["offset"] = size
                    with open_url(release["url"], **kwargs) as response:
                        if size:
                            content_range = getattr(response, "headers", {}).get(
                                "Content-Range", ""
                            )
                            expected_range = f"bytes {size}-{release['size'] - 1}/{release['size']}"
                            status = getattr(response, "status", 200)
                            if status != 206 or content_range != expected_range:
                                # A server may ignore Range; restart rather than append incorrect bytes.
                                output.seek(0)
                                output.truncate()
                                size = 0
                                digest = hashlib.sha512()
                                if status == 206:
                                    raise OSError("Invalid partial download response")
                        while chunk := response.read(1024 * 1024):
                            size += len(chunk)
                            if size > release["size"]:
                                raise LauncherError("Download exceeded its expected size")
                            digest.update(chunk)
                            output.write(chunk)
                            progress.update(size * 100 // release["size"])
                    if size == release["size"]:
                        break
                    raise OSError("Incomplete download")
                except (OSError, EOFError, http.client.HTTPException) as exc:
                    log.warning("Download attempt %s/%s failed: %s", attempt + 1, attempts, exc)
                    if attempt + 1 == attempts:
                        raise LauncherError("Download failed after retries") from exc
        if size != release["size"] or digest.hexdigest() != release["sha512"]:
            raise LauncherError("DEB size/checksum mismatch; existing installation was not changed")
    finally:
        progress.close()


def unpack_deb(deb: Path, destination: Path) -> None:
    """Read Debian's ar container without running maintainer scripts or binaries."""
    with deb.open("rb") as source, tempfile.TemporaryDirectory(dir=destination.parent) as work:
        if source.read(8) != b"!<arch>\n":
            raise LauncherError("Download is not a Debian package")
        members: dict[str, Path] = {}
        total = 0
        while header := source.read(60):
            if len(header) != 60 or header[58:] != b"`\n":
                raise LauncherError("Invalid Debian archive header")
            try:
                name = header[:16].decode("ascii").strip().rstrip("/")
                size = int(header[48:58])
            except (UnicodeError, ValueError) as exc:
                raise LauncherError("Invalid Debian archive member") from exc
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", name):
                raise LauncherError(f"Unsupported Debian archive member: {name}")
            if name in members or not 0 <= size <= MAX_DOWNLOAD:
                raise LauncherError("Invalid or duplicate Debian archive member")
            total += size
            if total > MAX_DOWNLOAD or len(members) >= 1000:
                raise LauncherError("Debian archive exceeds member limits")
            target = Path(work) / name
            with target.open("xb") as output:
                copy_exact(source, output, size)
            if size % 2 and source.read(1) != b"\n":
                raise LauncherError("Invalid Debian archive padding")
            members[name] = target
        if "debian-binary" not in members or members["debian-binary"].read_bytes() != b"2.0\n":
            raise LauncherError("Unsupported Debian package format")
        payloads = [
            path
            for name, path in members.items()
            if name == "data.tar" or name.startswith("data.tar.")
        ]
        if len(payloads) != 1:
            raise LauncherError("Missing or ambiguous Debian payload")
        extract_payload(payloads[0], destination)


def copy_exact(source: BinaryIO, output: BinaryIO, remaining: int) -> None:
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            raise LauncherError("Truncated Debian archive")
        output.write(chunk)
        remaining -= len(chunk)


def extract_payload(payload: Path, destination: Path) -> None:
    total = 0
    count = 0
    with tarfile.open(payload, "r:*") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise LauncherError("Unsafe path in Debian payload")
            count += 1
            total += member.size
            if total > MAX_EXTRACTED or count > 50000:
                raise LauncherError("Debian payload exceeds extraction limits")
            # Inspect every path, including entries outside the application subtree.
            safe = tarfile.data_filter(member, str(destination))
            if safe is None:
                continue
            name = safe.name.removeprefix("./")
            if not (
                name == "opt/Paper"
                or name.startswith("opt/Paper/")
                or name == "usr/share/icons/hicolor/256x256/apps/paper-desktop.png"
            ):
                continue
            if safe.issym():
                if not name.startswith("opt/Paper/") or os.path.isabs(safe.linkname):
                    raise LauncherError("Unsafe symlink in Paper payload")
                target = (destination / name).parent / safe.linkname
                if not target.resolve().is_relative_to((destination / "opt/Paper").resolve()):
                    raise LauncherError("Unsafe symlink in Paper payload")
            elif not (safe.isfile() or safe.isdir()):
                raise LauncherError("Unexpected hard link or special file in Paper payload")
            archive.extract(safe, destination, filter="data")
    executable = destination / "opt/Paper/paper-desktop"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise LauncherError("DEB does not contain an executable Paper application")


def show_error(message: str) -> None:
    """Report errors in the terminal and, when available, on the desktop."""
    print(f"paper-launcher: {message}", file=sys.stderr)
    if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("zenity"):
        try:
            subprocess.run(
                ["zenity", "--error", "--no-markup", "--title=Paper Launcher", "--text", message],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass


class Config(TypedDict):
    extra_arguments: list[str]
    extra_env_vars: dict[str, str]
    download_attempts: int
    timeout: int


def load_config() -> Config:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not config_home.is_absolute():
        raise LauncherError("XDG_CONFIG_HOME must be an absolute path")
    paths = (config_home / "paper-launcher.conf", Path("/etc/paper-launcher.conf"))
    data: dict[str, Any] = {}
    for path in paths:
        try:
            with path.open("rb") as source:
                data = tomllib.load(source)
            log.info("Loaded configuration from %s", path)
            break
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise LauncherError(f"Cannot load configuration {path}: {exc}") from exc
    section = data.get("paper", {})
    if not isinstance(section, dict):
        raise LauncherError("Configuration [paper] must be a table")
    arguments = section.get("extra_arguments", [])
    environment = section.get("extra_env_vars", [])
    attempts = section.get("download_attempts", 5)
    timeout = section.get("timeout", 30)
    if not isinstance(arguments, list) or not all(isinstance(x, str) for x in arguments):
        raise LauncherError("extra_arguments must be an array of strings")
    if not isinstance(environment, list) or not all(
        isinstance(x, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^\x00]*", x)
        for x in environment
    ):
        raise LauncherError("extra_env_vars must be an array of NAME=value strings")
    if any("\x00" in x for x in arguments):
        raise LauncherError("Arguments cannot contain NUL bytes")
    if type(attempts) is not int or not 1 <= attempts <= 100:
        raise LauncherError("download_attempts must be between 1 and 100")
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise LauncherError("timeout must be between 1 and 3600 seconds")
    return {
        "extra_arguments": arguments,
        "extra_env_vars": dict(x.split("=", 1) for x in environment),
        "download_attempts": attempts,
        "timeout": timeout,
    }


class Installation:
    def __init__(self, root: Path):
        self.root = root
        self.releases = root / "releases"
        self.state_path = root / "state.json"
        self.invalid_state: bytes | None = None

    def read_state(self) -> State:
        try:
            content = self.state_path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            return self.parse_state(json.loads(content))
        except (ValueError, UnicodeError, LauncherError) as exc:
            return self.recover_state(content, exc)

    def recover_state(self, content: bytes, error: Exception) -> State:
        candidates: list[InstalledRelease] = []
        if self.releases.is_dir():
            for path in self.releases.iterdir():
                if (
                    path.is_symlink()
                    or not path.is_dir()
                    or not re.fullmatch(r"[a-f0-9]{128}", path.name)
                ):
                    continue
                marker = path / ".paper-launcher-release"
                try:
                    if marker.is_symlink() or marker.read_text() != path.name:
                        continue
                    candidate: InstalledRelease = {"id": path.name, "version": "unknown"}
                    if self.executable(candidate):
                        candidates.append(candidate)
                except (OSError, UnicodeError):
                    continue
        if len(candidates) != 1:
            raise LauncherError(
                "Invalid launcher state and no unambiguous installed release; restore state.json from backup"
            ) from error
        self.invalid_state = content
        log.warning(
            "Invalid launcher state; recovered the only marked installation (version unknown)"
        )
        return {"current": candidates[0]}

    @staticmethod
    def parse_state(state: Any) -> State:
        if not isinstance(state, dict):
            raise LauncherError("Invalid launcher state")
        # Ignore obsolete state from launcher versions with manual release switching.
        state.pop("previous", None)
        state.pop("updates_paused", None)
        entry = state.get("current")
        if entry is not None and (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not re.fullmatch(r"[a-f0-9]{128}", entry["id"])
            or not isinstance(entry.get("version"), str)
        ):
            raise LauncherError("Invalid installed release in launcher state")
        for key in ("last_check", "last_attempt"):
            if key in state and (
                type(state[key]) not in (int, float)
                or not math.isfinite(state[key])
                or state[key] < 0
            ):
                raise LauncherError("Invalid update timestamp in launcher state")
        return cast(State, state)

    def save_state(self, state: State) -> None:
        # One atomic pointer update selects the validated release.
        if self.invalid_state is not None:
            with tempfile.NamedTemporaryFile(
                prefix="state.invalid-", dir=self.root, delete=False
            ) as backup:
                backup.write(self.invalid_state)
                backup.flush()
                os.fsync(backup.fileno())
            log.warning("Saved invalid launcher state to %s", backup.name)
            self.invalid_state = None
        with tempfile.NamedTemporaryFile(mode="w", dir=self.root, delete=False) as output:
            pending = Path(output.name)
            try:
                json.dump(state, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
                os.replace(pending, self.state_path)
            finally:
                pending.unlink(missing_ok=True)

    def executable(self, entry: InstalledRelease | None) -> Path | None:
        if entry is None:
            return None
        path = self.releases / entry["id"] / "opt/Paper/paper-desktop"
        return path if path.is_file() and os.access(path, os.X_OK) else None

    def update(
        self, state: State, arch: str, *, force: bool = False, attempts: int = 5, timeout: int = 30
    ) -> None:
        now = time.time()
        if not force and self.executable(state.get("current")):
            if 0 <= now - state.get("last_check", 0) < CHECK_INTERVAL:
                return
        release = release_metadata(arch)
        current = state.get("current")
        if current and current["id"] == release["sha512"] and self.executable(current):
            current["version"] = release["version"]
            state.update(last_check=now)
            self.save_state(state)
            self.prune(state)
            return
        log.info("Downloading Paper %s", release["version"])
        self.releases.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=self.releases) as work:
            staging = Path(work)
            deb = staging / "paper.deb"
            download(release, deb, attempts=attempts, timeout=timeout)
            log.info("Installing Paper %s", release["version"])
            extracted = staging / "root"
            extracted.mkdir()
            unpack_deb(deb, extracted)
            (extracted / ".paper-launcher-release").write_text(release["sha512"])
            target = self.releases / release["sha512"]
            if target.exists():
                marker = target / ".paper-launcher-release"
                cached: InstalledRelease = {"id": release["sha512"], "version": release["version"]}
                if (
                    target.is_symlink()
                    or not marker.is_file()
                    or marker.read_text() != release["sha512"]
                    or not self.executable(cached)
                ):
                    raise LauncherError(
                        "Existing cached release is incomplete; refusing to overwrite it"
                    )
            else:
                extracted.rename(target)
            next_state: State = {
                **state,
                "current": {"id": release["sha512"], "version": release["version"]},
                "last_check": now,
            }
            self.save_state(next_state)
            state.update(next_state)
            icon = target / "usr/share/icons/hicolor/256x256/apps/paper-desktop.png"
            if icon.is_file():
                try:
                    temporary_icon = self.root / "icon.png.new"
                    shutil.copyfile(icon, temporary_icon)
                    temporary_icon.replace(self.root / "icon.png")
                except OSError as exc:
                    print(f"paper-launcher: Could not update the menu icon: {exc}", file=sys.stderr)
            self.prune(state)
        log.info("Paper %s is ready", release["version"])

    def prune(self, state: State) -> None:
        current = state.get("current")
        keep = {current["id"]} if current else set()
        for path in self.releases.iterdir():
            # Delete only this launcher's marked, generated release directories.
            if (
                path.name in keep
                or not re.fullmatch(r"[a-f0-9]{128}", path.name)
                or path.is_symlink()
            ):
                continue
            marker = path / ".paper-launcher-release"
            try:
                if marker.is_file() and marker.read_text() == path.name:
                    shutil.rmtree(path)
            except OSError as exc:
                print(f"paper-launcher: Could not remove old release: {exc}", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    actions = result.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="check upstream without installing")
    actions.add_argument("--update", action="store_true", help="install latest release and exit")
    actions.add_argument(
        "--status", action="store_true", help="show installed version without network access"
    )
    actions.add_argument(
        "--skip-update", action="store_true", help="launch installed Paper without checking"
    )
    actions.add_argument(
        "--check-update", action="store_true", help="check for updates now, then launch"
    )
    actions.add_argument(
        "--print-deb-url", action="store_true", help="print the latest package URL and exit"
    )
    result.add_argument(
        "--no-exec", action="store_true", help="show the launch command without launching Paper"
    )
    result.add_argument(
        "-v", "--verbose", action="count", default=0, help="increase diagnostic logging"
    )
    result.add_argument("--download-attempts", type=int, help="maximum download attempts (1–100)")
    result.add_argument(
        "--timeout", type=int, help="download connection timeout in seconds (1–3600)"
    )
    result.add_argument("--version", action="version", version=f"paper-launcher {VERSION}")
    result.add_argument("paper_args", nargs=argparse.REMAINDER, help="Paper arguments, after --")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    if os.geteuid() == 0:
        raise LauncherError("Run paper-launcher as your normal user, not root")
    if not shutil.which("zenity"):
        raise LauncherError("Zenity is required; install the zenity package")
    config = load_config()
    attempts = (
        args.download_attempts
        if args.download_attempts is not None
        else config["download_attempts"]
    )
    timeout = args.timeout if args.timeout is not None else config["timeout"]
    if not 1 <= attempts <= 100 or not 1 <= timeout <= 3600:
        raise LauncherError("Download attempts must be 1–100 and timeout must be 1–3600 seconds")
    if args.print_deb_url:
        print(release_metadata(architecture())["url"])
        return 0
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    if not data.is_absolute():
        raise LauncherError("XDG_DATA_HOME must be an absolute path")
    installation = Installation(data / "paper-launcher")
    installation.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The lock is passed to Paper so updates cannot replace files while it runs.
    with (installation.root / "launcher.lock").open("a+") as lock:
        exclusive = True
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            exclusive = False
            if args.update or args.check_update:
                raise LauncherError(
                    "Paper or its updater is running. Close it before updating."
                ) from None
            fcntl.flock(lock, fcntl.LOCK_SH)
        state = installation.read_state()
        if args.status:
            current = state.get("current")
            print(f"Installed: {current['version'] if current else 'none'}")
            print(f"Data: {installation.root}")
            return 0
        if args.check:
            release = release_metadata(architecture())
            print(f"Latest: {release['version']}")
            print(
                "Up to date"
                if (state.get("current") or {}).get("id") == release["sha512"]
                else "Update available; run paper-launcher --update"
            )
            return 0
        if exclusive and not args.skip_update:
            try:
                installation.update(
                    state,
                    architecture(),
                    force=args.update or args.check_update,
                    attempts=attempts,
                    timeout=timeout,
                )
            except (LauncherError, OSError, ValueError, tarfile.TarError) as exc:
                if args.update or not installation.executable(state.get("current")):
                    raise LauncherError(f"Update failed: {exc}") from exc
                show_error(f"Update failed; opening installed Paper. {exc}")
        if args.update:
            return 0
        executable = installation.executable(state.get("current"))
        if executable is None:
            raise LauncherError("Paper is not installed. Run paper-launcher --update while online.")
        fcntl.flock(lock, fcntl.LOCK_SH)
        paper_args = args.paper_args
        if paper_args[:1] == ["--"]:
            paper_args = paper_args[1:]
        paper_args = [*config["extra_arguments"], *paper_args]
        # ToDesktop disables its Linux updater outside AppImage. Do not let an
        # AppImage-based terminal/parent falsely identify this extracted DEB.
        environment = os.environ.copy()
        environment.update(config["extra_env_vars"])
        environment.pop("APPIMAGE", None)
        environment.pop("APPDIR", None)
        if args.no_exec:
            import shlex

            print(shlex.join([str(executable), *paper_args]))
            return 0
        # No shell interpolation, no --no-sandbox fallback, and keep argv URLs intact.
        os.chdir(executable.parent)
        os.set_inheritable(lock.fileno(), True)
        os.execve(executable, [str(executable), *paper_args], environment)


def entrypoint() -> None:
    try:
        sys.exit(main())
    except (LauncherError, OSError, ValueError, tarfile.TarError) as exc:
        show_error(str(exc))
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    entrypoint()
