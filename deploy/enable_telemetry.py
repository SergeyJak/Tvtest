#!/usr/bin/env python3
"""Safely opt in to TV telemetry on an existing systemd IPTV installation.

Keeps the existing ExecStart settings (including EPG, HTTPS, channel/token
config), backups, validates effective unit and rolls back on failure.
Run once as root after reviewing the patch. Briefly restarts IPTV gateway.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

UNIT = "iptv-gateway.service"
FLAG = "--telemetry-db"
DB_PATH = "/home/client_911_1/Tvtest/.iptv_state/telemetry.sqlite3"


def get_property(name: str) -> str:
    return subprocess.check_output(
        ["systemctl", "show", "-P", name, UNIT], text=True
    ).strip()


def add_option(text: str, option: str) -> str:
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines)
              if line.lstrip().startswith("ExecStart=") and
              line.split("=", 1)[1].strip()]
    if len(starts) != 1:
        raise ValueError("Expected exactly one nonempty ExecStart in selected file")
    start = starts[0]
    end = start
    while lines[end].rstrip().endswith("\\"):
        end += 1
        if end >= len(lines):
            raise ValueError("Incomplete multiline ExecStart")
    block = "".join(lines[start:end + 1])
    if "iptv_multi.py" not in block:
        raise ValueError("ExecStart does not launch iptv_multi.py")
    if FLAG in block:
        return text
    lines[end] = lines[end].rstrip("\r\n") + " " + option + "\n"
    return "".join(lines)


def choose_unit_file() -> Path:
    paths = get_property("DropInPaths").split()
    effective = []
    for candidate in paths:
        path = Path(candidate)
        if path.exists() and "ExecStart=" in path.read_text():
            effective.append(path)
    if effective:
        return effective[-1]
    return Path(get_property("FragmentPath"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    effective_before = get_property("ExecStart")
    if "iptv_multi.py" not in effective_before or "--epg-file" not in effective_before:
        parser.error("Unexpected effective gateway command; no changes made")
    if FLAG in effective_before:
        print("Telemetry already enabled")
        return
    path = choose_unit_file()
    old = path.read_text()
    new = add_option(old, f"{FLAG} {DB_PATH}")
    if new == old:
        print("File already contains telemetry; check existing override priority")
        return
    if args.dry_run:
        print(f"Would modify {path}; add {FLAG} {DB_PATH} to ExecStart")
        return
    if __import__("os").geteuid() != 0:
        parser.error("Root permission required")
    backup = path.with_name(path.name + ".bak-telemetry")
    if backup.exists():
        parser.error(f"Existing backup {backup}; refusing to overwrite it")
    shutil.copy2(path, backup)
    try:
        path.write_text(new)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemd-analyze", "verify", UNIT], check=True)
        effective_after = get_property("ExecStart")
        if FLAG not in effective_after or "--epg-file" not in effective_after:
            raise RuntimeError("Effective systemd command was not updated")
        subprocess.run(["systemctl", "restart", UNIT], check=True)
        subprocess.run(["systemctl", "is-active", "--quiet", UNIT], check=True)
    except Exception:
        shutil.copy2(backup, path)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "restart", UNIT], check=False)
        raise
    print("Telemetry enabled; gateway active; original config backed up.")
    print("Note: usage data starts accumulating only from this point forward.")


if __name__ == "__main__":
    main()
