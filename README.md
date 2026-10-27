# Paper Launcher

A small, unofficial Linux launcher for [Paper Desktop](https://paper.design), inspired by
spotify-launcher. No AppImage, systemd timer, WebKit build, AUR publication, or root access.

## Install (Arch Linux)

Required packages: `python`, `python-yaml`, `gtk3`, `nss`, `libxss`, `libxtst`, `libnotify`,
`libsecret`, `xdg-utils`, `at-spi2-core`, `util-linux-libs`, and `alsa-lib`.
Install any missing packages through your normal Arch package manager first.
`libappindicator` is optional for tray integration.

From this directory, run as your regular user:

```sh
python install.py --register-protocol
```

This installs `~/.local/bin/paper-launcher` and a **Paper** application-menu entry.
`--register-protocol` makes it the default `paper://` handler for browser sign-in;
omit the flag if you want to preserve your current handler. Add `~/.local/bin` to
your shell's `PATH` if it is not there already.

Open **Paper** from the application menu. The first launch downloads and extracts
the official DEB into your home folder. Nothing from the DEB's maintainer scripts runs.
First-time installation needs an internet connection and approximately 1 GB of free
space; later updates temporarily need additional space for staging.

If another Paper installation is present, its menu entry may also appear. This installer
does not remove or modify it. Avoid launching that installation alongside this one.

## Use

```sh
paper-launcher                  # Check if due, install an update, launch Paper
paper-launcher --check          # Report latest upstream version; do not install
paper-launcher --update         # Check and install now, then exit
paper-launcher --skip-update    # Open installed Paper without checking
paper-launcher --status         # Installed version and data path
paper-launcher -- --ozone-platform=wayland  # Forward Electron arguments
```

Successful checks are limited to once per 24 hours. Failed checks retry no sooner than
one hour later, while an already installed version still opens offline. An explicit
`--update` bypasses these intervals.

Updates require Paper to be closed. The launcher holds a shared file lock
throughout the child application's lifetime. Another launch can forward a URL to the
running app, but cannot update its files. Updates are staged and validated before one
atomic state-file replacement selects the new installation. Failed updates leave the
working installation intact; after a successful update, old launcher-marked releases
are removed. Paper's documents and profile are not modified by the launcher.

Notifications use `notify-send` when available; diagnostics also appear on stderr.
If Paper fails to start, launch it from a terminal to see Electron's output.
The launcher **never automatically adds `--no-sandbox`**. A system that disables
unprivileged user namespaces may need administrator configuration for Electron's sandbox.

## Update and security model

- The architecture-specific ToDesktop feed is identified by `resources/app-update.yml`
  inside Paper's official DEB. Both x86-64 and ARM64 are supported by the launcher;
  the real package integration test was performed on x86-64 only.
- Metadata and packages are fetched over certificate-verified HTTPS, including redirects.
  The launcher accepts only simple versioned DEB filenames under the fixed feed URL.
- Downloads must match the feed's exact byte count and SHA-512 checksum before extraction.
  **The feed is not independently signed:** this protects transfer integrity, not against
  compromise of Paper's release infrastructure.
- Extraction rejects unsafe paths, special files, and application symlinks, and strips
  setuid/setgid bits. All files remain owned by your normal user.
- Paper's current ToDesktop runtime disables its Linux updater outside AppImage.
  The launcher clears inherited `APPIMAGE`/`APPDIR` variables before starting Paper,
  so an AppImage-based parent application cannot accidentally enable that updater.
  The upstream application resources are otherwise preserved.
- This is not a sandbox for Paper itself: like a normal desktop app, it runs with your
  user's access. Review and trust the upstream application before using it.
- Launcher code and system dependencies do not self-update. Re-run `install.py` after
  updating this source; keep system dependencies current through pacman.

State, releases, and the installed launcher module live in
`${XDG_DATA_HOME:-~/.local/share}/paper-launcher`. User configuration and documents
remain wherever Paper itself stores them. Do not edit state.json while Paper is running.

## Uninstall

Close Paper. Remove only `~/.local/bin/paper-launcher`,
`${XDG_DATA_HOME:-~/.local/share}/applications/paper-launcher.desktop`, and
`${XDG_DATA_HOME:-~/.local/share}/paper-launcher`.
If you registered the protocol, select another `paper://` handler in your desktop's
default-app settings. Uninstalling this launcher does not delete Paper's profile/documents.

## Development

```sh
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
pyright
```

Tests use generated DEBs and fake network responses. They do not launch the real Paper
application, touch your installed apps, or require network access.
