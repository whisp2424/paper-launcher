#!/usr/bin/env python3
"""User-local Paper installer and launch-time updater. Linux, Python 3.12+."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
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
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, TypedDict, cast

import yaml

# Taken from resources/app-update.yml in Paper's official Linux DEB.
RELEASE_BASE = "https://download.todesktop.com/2601167vjw8xe/"
CHECK_INTERVAL = 24 * 60 * 60
RETRY_INTERVAL = 60 * 60
MAX_DOWNLOAD = 1024 * 1024 * 1024
MAX_EXTRACTED = 2 * MAX_DOWNLOAD
VERSION = "0.1.0"


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


def open_url(url: str, timeout: int = 15):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise LauncherError("Downloads must use HTTPS")
    opener = urllib.request.build_opener(HTTPSRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": f"paper-launcher/{VERSION}"})
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
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version):
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
        if not re.fullmatch(r"paper-desktop-[A-Za-z0-9.+_-]+\.deb", name):
            raise LauncherError("Unsafe DEB URL in update metadata")
        if not name.startswith(f"paper-desktop-{version}-"):
            raise LauncherError("Release filename does not match its version")
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


def download(release: Release, destination: Path) -> None:
    digest = hashlib.sha512()
    size = 0
    deadline = time.monotonic() + 15 * 60
    with open_url(release["url"], timeout=30) as response, destination.open("xb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > release["size"] or time.monotonic() > deadline:
                raise LauncherError("Download exceeded its expected size or time limit")
            digest.update(chunk)
            output.write(chunk)
    if size != release["size"] or digest.hexdigest() != release["sha512"]:
        raise LauncherError("DEB size/checksum mismatch; existing installation was not changed")


def unpack_deb(deb: Path, destination: Path) -> None:
    """Read Debian's ar container without running maintainer scripts or binaries."""
    with deb.open("rb") as source, tempfile.TemporaryDirectory(dir=destination.parent) as work:
        if source.read(8) != b"!<arch>\n":
            raise LauncherError("Download is not a Debian package")
        members: dict[str, Path] = {}
        while header := source.read(60):
            if len(header) != 60 or header[58:] != b"`\n":
                raise LauncherError("Invalid Debian archive header")
            try:
                name = header[:16].decode("ascii").strip().rstrip("/")
                size = int(header[48:58])
            except (UnicodeError, ValueError) as exc:
                raise LauncherError("Invalid Debian archive member") from exc
            if not re.fullmatch(r"(?:debian-binary|(?:control|data)\.tar\.(?:xz|gz|bz2))", name):
                raise LauncherError(f"Unsupported Debian archive member: {name}")
            if name in members or not 0 <= size <= MAX_DOWNLOAD:
                raise LauncherError("Invalid or duplicate Debian archive member")
            target = Path(work) / name
            with target.open("xb") as output:
                copy_exact(source, output, size)
            if size % 2 and source.read(1) != b"\n":
                raise LauncherError("Invalid Debian archive padding")
            members[name] = target
        if "debian-binary" not in members or members["debian-binary"].read_bytes() != b"2.0\n":
            raise LauncherError("Unsupported Debian package format")
        payloads = [path for name, path in members.items() if name.startswith("data.tar.")]
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
            # Paper's current DEB has no links. Fail closed if that changes.
            if not (safe.isfile() or safe.isdir()):
                raise LauncherError("Unexpected link or special file in Paper payload")
            archive.extract(safe, destination, filter="data")
    executable = destination / "opt/Paper/paper-desktop"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise LauncherError("DEB does not contain an executable Paper application")


def notify(message: str, *, error: bool = False) -> None:
    print(f"paper-launcher: {message}", file=sys.stderr)
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                [
                    "notify-send",
                    "--app-name=Paper Launcher",
                    "--urgency=" + ("critical" if error else "normal"),
                    "Paper",
                    message,
                ],
                timeout=3,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


class Installation:
    def __init__(self, root: Path):
        self.root = root
        self.releases = root / "releases"
        self.state_path = root / "state.json"

    def read_state(self) -> State:
        try:
            state = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}
        except (ValueError, UnicodeError) as exc:
            raise LauncherError("Invalid launcher state; restore state.json from backup") from exc
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

    def update(self, state: State, arch: str, *, force: bool = False) -> None:
        now = time.time()
        if not force and self.executable(state.get("current")):
            if now - state.get("last_check", 0) < CHECK_INTERVAL:
                return
            if now - state.get("last_attempt", 0) < RETRY_INTERVAL:
                return
        state["last_attempt"] = now
        self.save_state(state)
        release = release_metadata(arch)
        current = state.get("current")
        if current and current["id"] == release["sha512"] and self.executable(current):
            state.update(last_check=now)
            self.save_state(state)
            self.prune(state)
            return
        notify(f"Downloading Paper {release['version']}…")
        self.releases.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=self.releases) as work:
            staging = Path(work)
            deb = staging / "paper.deb"
            download(release, deb)
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
        notify(f"Paper {release['version']} is ready")

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
    result.add_argument("--version", action="version", version=f"paper-launcher {VERSION}")
    result.add_argument("paper_args", nargs=argparse.REMAINDER, help="Paper arguments, after --")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if os.geteuid() == 0:
        raise LauncherError("Run paper-launcher as your normal user, not root")
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    if not data.is_absolute():
        raise LauncherError("XDG_DATA_HOME must be an absolute path")
    installation = Installation(data / "paper-launcher")
    installation.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Held until the child exits: updates never replace files beneath a running app.
    with (installation.root / "launcher.lock").open("a+") as lock:
        exclusive = True
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            exclusive = False
            if args.update:
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
                installation.update(state, architecture(), force=args.update)
            except (LauncherError, OSError, ValueError, tarfile.TarError) as exc:
                if args.update or not installation.executable(state.get("current")):
                    raise LauncherError(f"Update failed: {exc}") from exc
                notify(f"Update failed; opening installed Paper. {exc}")
        if args.update:
            return 0
        executable = installation.executable(state.get("current"))
        if executable is None:
            raise LauncherError("Paper is not installed. Run paper-launcher --update while online.")
        fcntl.flock(lock, fcntl.LOCK_SH)
        paper_args = args.paper_args
        if paper_args[:1] == ["--"]:
            paper_args = paper_args[1:]
        # ToDesktop disables its Linux updater outside AppImage. Do not let an
        # AppImage-based terminal/parent falsely identify this extracted DEB.
        environment = os.environ.copy()
        environment.pop("APPIMAGE", None)
        environment.pop("APPDIR", None)
        # No shell interpolation, no --no-sandbox fallback, and keep argv URLs intact.
        return subprocess.call(
            [str(executable), *paper_args],
            cwd=executable.parent,
            pass_fds=(lock.fileno(),),
            env=environment,
        )


def entrypoint() -> None:
    try:
        sys.exit(main())
    except (LauncherError, OSError, ValueError, tarfile.TarError) as exc:
        notify(str(exc), error=True)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    entrypoint()
