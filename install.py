#!/usr/bin/env python3
"""Install the launcher and desktop entry for the current user (no root)."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

MARKER = "# Managed by paper-launcher installer"


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), mode)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def desktop_quote(path: Path) -> str:
    text = str(path)
    if any(ord(character) < 32 for character in text):
        raise ValueError("Installation paths cannot contain control characters")
    text = (
        text.replace("\\", "\\\\\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    )
    return '"' + text.replace("%", "%%") + '"'


def install(*, register_protocol: bool = False) -> None:
    if os.geteuid() == 0:
        raise ValueError("Run the installer as your regular user, not root")
    import yaml  # noqa: F401 -- verify the runtime dependency before installing

    if not shutil.which("zenity"):
        raise ValueError("Zenity is required; install the zenity package first")

    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share")))
    if not data.is_absolute():
        raise ValueError("XDG_DATA_HOME must be absolute")
    root = data / "paper-launcher"
    launcher = home / ".local/bin/paper-launcher"
    module = root / "launcher.py"
    desktop = data / "applications/paper-launcher.desktop"
    destinations = (launcher, module, desktop)
    for path in destinations:
        if path.is_symlink() or (path.exists() and MARKER not in path.read_text()):
            raise ValueError(f"Refusing to overwrite an unmanaged file: {path}")
    command = desktop_quote(launcher)
    entry = f"""{MARKER}
[Desktop Entry]
Type=Application
Name=Paper
Comment=Paper Desktop (managed by Paper Launcher)
Exec={command} -- %U
Icon={root}/icon.png
Terminal=false
StartupWMClass=Paper
Categories=Graphics;
MimeType=x-scheme-handler/paper;
X-Paper-Launcher=true
"""
    # Validate paths before writing any files.
    desktop_quote(root)
    source = Path(__file__).with_name("paper_launcher.py").read_text()
    atomic_write(module, MARKER + "\n" + source)
    atomic_write(
        launcher,
        f"#!/usr/bin/env python3\n{MARKER}\nimport runpy\nrunpy.run_path({str(module)!r}, run_name='__main__')\n",
        0o755,
    )
    atomic_write(desktop, entry)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(desktop.parent)], check=False)
    if register_protocol:
        if not shutil.which("xdg-mime"):
            raise ValueError(
                "Launcher installed, but xdg-mime is missing; paper:// handler was not registered"
            )
        subprocess.run(["xdg-mime", "default", desktop.name, "x-scheme-handler/paper"], check=True)
    print(f"Installed {launcher}\nOpen Paper from your application menu to download and launch it.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--register-protocol",
        action="store_true",
        help="make this launcher the default paper:// handler",
    )
    options = parser.parse_args()
    try:
        install(register_protocol=options.register_protocol)
    except (OSError, ValueError, ImportError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Installation failed: {exc}\n")
