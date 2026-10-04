"""Read text from the system clipboard without extra dependencies."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


class ClipboardError(Exception):
    pass


def _commands() -> list[list[str]]:
    if sys.platform == "darwin":
        return [["pbpaste"]]
    if sys.platform == "win32":
        return [["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"]]
    commands = [
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ]
    if os.environ.get("WAYLAND_DISPLAY"):
        commands.insert(0, ["wl-paste", "--no-newline"])
    return commands


def read_clipboard() -> str:
    """Return the clipboard's text contents (possibly empty)."""
    # pbpaste transcodes to the locale's charset, so make sure that's UTF-8.
    env = {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
    tried = []
    for command in _commands():
        if not shutil.which(command[0]):
            tried.append(command[0])
            continue
        try:
            done = subprocess.run(command, capture_output=True, env=env, timeout=5, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            raise ClipboardError(f"Couldn't read the clipboard with {command[0]}: {e}") from e
        return done.stdout.decode("utf-8", errors="replace")
    raise ClipboardError(f"No clipboard tool found (looked for {', '.join(tried)}).")
