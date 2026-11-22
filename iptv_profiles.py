"""Three private, independent TV profiles for a single administration panel.

Source URLs and playlist snapshots stay in owner-only VPS files. Public M3U/EPG
URLs use individual random tokens; only authenticated admins may retrieve them.
The original primary profile preserves the existing gateway token and playlist.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import socket
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import ipaddress
from pathlib import Path

from iptv_gateway import load_or_create_token, private_playlist_text
from iptv_healthcheck import UA

PROFILE_COUNT = 3
MAX_SOURCE_SIZE = 2 * 1024 * 1024
DEFAULT_NAMES = ["Основной", "Профиль 2", "Профиль 3"]
EPG_PATTERN = re.compile(r"""(?:x-tvg-url|url-tvg)\s*=\s*(["'])(.*?)\1""", re.I)


class ProfileError(ValueError):
    """Safe error text that never includes provider credentials."""


def validate_source_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ProfileError("Некорректный URL")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProfileError("Требуется публичная HTTP/HTTPS ссылка")
    if parsed.username or parsed.password or parsed.fragment:
        raise ProfileError("URL не должен содержать логин, пароль или #фрагмент")
    host = parsed.hostname
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        raise ProfileError("Неподдерживаемый домен источника")
    if host.lower() == "localhost" or host.endswith(".local"):
        raise ProfileError("Локальный источник запрещён")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                        type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        raise ProfileError("Адрес источника не найден") from None
    if not addresses:
        raise ProfileError("Адрес источника не найден")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except ValueError:
            raise ProfileError("Некорректный адрес источника") from None
        if not ip.is_global:
            raise ProfileError("Локальные и приватные адреса запрещены")
    return url.strip()


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_source_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_playlist(url: str, timeout: float = 18) -> str:
    """Limited public web download, never report URL or exception repr to caller."""
    validate_source_url(url)
    opener = urllib.request.build_opener(SafeRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": UA}),
                         timeout=timeout) as response:
            if response.geturl() != url:
                validate_source_url(response.geturl())
            body = response.read(MAX_SOURCE_SIZE + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProfileError("Не удалось загрузить M3U (сеть или HTTP)") from None
    if len(body) > MAX_SOURCE_SIZE:
        raise ProfileError("M3U слишком большой (лимит 2 МБ)")
    try:
        contents = body.decode("utf-8-sig")
    except UnicodeError:
        raise ProfileError("M3U должен быть в UTF-8") from None
    if not contents.lstrip().startswith("#EXTM3U"):
        raise ProfileError("Источник не является M3U-плейлистом")
    return contents


def extract_epg_source(playlist: str, *, validate: bool = True) -> str | None:
    header = playlist.splitlines()[0] if playlist.splitlines() else ""
    match = EPG_PATTERN.search(header)
    if not match:
        return None
    source = match.group(2).split(",")[0].strip()
    if not source:
        return None
    if not validate:
        parsed = urllib.parse.urlsplit(source)
        return source if parsed.scheme in ("http", "https") and parsed.hostname else None
    try:
        return validate_source_url(source)
    except ProfileError:
        return None


def atomic_private_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ProfileError("Небезопасный путь файла")
    fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data.encode("utf-8") if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ProfileStore:
    def __init__(self, directory: Path, legacy_playlist: Path,
                 legacy_epg: Path | None, legacy_token_file: Path):
        self.directory = Path(directory)
        self.legacy_playlist = Path(legacy_playlist)
        self.legacy_epg = Path(legacy_epg) if legacy_epg is not None else None
        self.legacy_token_file = Path(legacy_token_file)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise ProfileError("Каталог профилей должен быть доступен только владельцу (chmod 700)")
        self.path = self.directory / "profiles.json"
        self.lock_path = self.directory / "profiles.lock"
        self._init_profiles()

    def snapshot_path(self, slot: int) -> Path:
        return self.directory / f"profile-{slot}.m3u8"

    def epg_path(self, slot: int) -> Path:
        return self.directory / f"profile-{slot}.epg.xml.gz"

    def epg_error_path(self, slot: int) -> Path:
        return self.directory / f"profile-{slot}.epg-error"

    @contextlib.contextmanager
    def _locked(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield

    def _default(self) -> dict:
        old_token = load_or_create_token(self.legacy_token_file)
        return {"version": 1, "profiles": [
            {"slot": i, "name": DEFAULT_NAMES[i - 1],
             "token": old_token if i == 1 else secrets.token_urlsafe(24),
             "source_url": "", "epg_url": "", "revision": 0,
             "updated_at": None}
            for i in range(1, PROFILE_COUNT + 1)
        ]}

    def _init_profiles(self) -> None:
        with self._locked():
            if not self.path.exists():
                atomic_private_write(self.path, json.dumps(self._default(), ensure_ascii=False))
            else:
                # Do not regenerate or rotate tokens when code is deployed.
                self._read()

    def _read(self) -> dict:
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise ProfileError("Небезопасные права доступа к профилям")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        rows = data.get("profiles", [])
        if len(rows) != PROFILE_COUNT or [row.get("slot") for row in rows] != [1, 2, 3]:
            raise ProfileError("Некорректный файл профилей")
        return data

    def all(self) -> list[dict]:
        return self._read()["profiles"]

    def get(self, slot: int) -> dict:
        if slot not in (1, 2, 3):
            raise ProfileError("Профиль не найден")
        return dict(self.all()[slot - 1])

    def configured(self, row: dict) -> bool:
        if row["slot"] == 1 and not row["source_url"]:
            return self.legacy_playlist.is_file()
        return bool(row["source_url"] and self.snapshot_path(row["slot"]).is_file())

    def media_path(self, row: dict) -> Path:
        if row["slot"] == 1 and not row["source_url"]:
            return self.legacy_playlist
        return self.snapshot_path(row["slot"])

    def guide_path(self, row: dict) -> Path | None:
        if row["slot"] == 1 and not row["source_url"]:
            return self.legacy_epg
        return self.epg_path(row["slot"])

    def guide_source(self, row: dict) -> str | None:
        if not row["source_url"]:
            return None
        if row["epg_url"]:
            return row["epg_url"]
        try:
            return extract_epg_source(self.snapshot_path(row["slot"]).read_text(encoding="utf-8-sig"), validate=False)
        except OSError:
            return None

    def public_profiles(self, base_url: str) -> list[dict]:
        rows = self.all()
        result = []
        for row in rows:
            slot = row["slot"]
            configured = self.configured(row)
            epg_path = self.guide_path(row)
            available_epg = bool(epg_path and epg_path.is_file())
            has_epg_source = bool(self.guide_source(row) or (slot == 1 and not row["source_url"]))
            preview = ""
            if row["source_url"]:
                preview = urllib.parse.urlsplit(row["source_url"]).hostname or ""
            base = f"{base_url.rstrip('/')}/{row['token']}"
            result.append({
                "slot": slot,
                "name": row["name"],
                "configured": configured,
                "source_host": preview,
                "updated_at": row.get("updated_at"),
                "epg_status": "ready" if available_epg else
                              "error" if self.epg_error_path(slot).exists() else
                              "pending" if configured and has_epg_source else "unavailable",
                "m3u": base + "/channels.m3u" if configured else None,
                "epg": base + "/epg.xml.gz" if configured and has_epg_source else None,
                "legacy": slot == 1 and not row["source_url"],
            })
        return result

    def update(self, slot: int, name: str, playlist_url: str | None = None,
               epg_url: str | None = None) -> tuple[dict, bool]:
        if slot not in (1, 2, 3):
            raise ProfileError("Профиль не найден")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 50:
            raise ProfileError("Имя должно содержать от 1 до 50 символов")
        name = name.strip()
        if any(ord(char) < 32 for char in name):
            raise ProfileError("Некорректные символы в названии")
        if playlist_url is not None and not isinstance(playlist_url, str):
            raise ProfileError("Некорректный M3U URL")
        if epg_url is not None and not isinstance(epg_url, str):
            raise ProfileError("Некорректный EPG URL")
        new_source = playlist_url.strip() if playlist_url else None
        new_epg = epg_url.strip() if epg_url else None
        if new_source is not None:
            playlist = download_playlist(new_source)
            # Import locally to avoid circular dependencies.
            from iptv_multi import channels_from_text
            channels = channels_from_text(playlist)
            if not channels or len({item.id for item in channels}) != len(channels):
                raise ProfileError("M3U не содержит корректных уникальных каналов")
            if new_epg is None:
                # Follow the current M3U header on every scheduled refresh,
                # instead of freezing a provider's rotating EPG URL.
                extract_epg_source(playlist)
            else:
                new_epg = validate_source_url(new_epg)
        elif new_epg is not None:
            new_epg = validate_source_url(new_epg)
        with self._locked():
            data = self._read()
            row = data["profiles"][slot - 1]
            changed = new_source is not None or new_epg is not None
            row["name"] = name
            if new_source is not None:
                atomic_private_write(self.snapshot_path(slot), playlist)
                row["source_url"] = new_source
                row["epg_url"] = new_epg or ""
                row["revision"] += 1
                # Never serve the old account's TV guide after switching sources.
                self.epg_path(slot).unlink(missing_ok=True)
                self.epg_error_path(slot).unlink(missing_ok=True)
            elif new_epg is not None:
                row["epg_url"] = new_epg
                row["revision"] += 1
                self.epg_path(slot).unlink(missing_ok=True)
            row["updated_at"] = int(time.time())
            atomic_private_write(self.path, json.dumps(data, ensure_ascii=False))
            return dict(row), changed
