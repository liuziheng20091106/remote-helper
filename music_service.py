"""Small, local NetEase Music client used by the Remote Helper service.

The request format follows the public musicbox ``weapi`` protocol.  Credentials
are deliberately kept in a separate JSON file and are only sent to NetEase API
hosts; CDN responses are downloaded without the account Cookie header.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import gzip
import hashlib
import json
import logging
import os
import secrets
import string
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from Cryptodome.Cipher import AES


_MAX_DOWNLOAD = 50 * 1024 * 1024
_MAX_CACHE = 500
_API_TIMEOUT = 15.0
_DOWNLOAD_TIMEOUT = 25.0
_NONCE = b"0CoJUm6Qyw8W8jud"
_IV = b"0102030405060708"
_EAPI_KEY = b"e82ckenh8dichen8"
_RSA_MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
    "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
    "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932"
    "575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b"
    "3ece0462db0a22b8e7",
    16,
)


class _MusicError(RuntimeError):
    pass


class _PlaybackCancelled(Exception):
    pass


def _aes_cbc(data: bytes, key: bytes) -> bytes:
    pad = 16 - len(data) % 16
    return AES.new(key, AES.MODE_CBC, _IV).encrypt(data + bytes([pad]) * pad)


def _weapi(payload: dict[str, Any]) -> dict[str, str]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    # musicbox base64-encodes the first AES result before the second AES pass.
    key = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16)).encode()
    inner = base64.b64encode(_aes_cbc(raw, _NONCE))
    params = base64.b64encode(_aes_cbc(inner, key)).decode()
    reversed_key = int.from_bytes(key[::-1], "big")
    encrypted_key = format(pow(reversed_key, 0x10001, _RSA_MODULUS), "x").zfill(256)
    return {"params": params, "encSecKey": encrypted_key}


def _pkcs7(data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


def _eapi(uri: str, payload: dict[str, Any]) -> dict[str, str]:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.md5(f"nobody{uri}use{text}md5forencrypt".encode()).hexdigest()
    plain = f"{uri}-36cd479b6b5-{text}-36cd479b6b5-{digest}".encode()
    return {"params": AES.new(_EAPI_KEY, AES.MODE_ECB).encrypt(_pkcs7(plain)).hex().upper()}


def _eapi_json(content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, ValueError):
        raw = content
        try:
            if raw and all(char in b"0123456789abcdefABCDEF" for char in raw):
                raw = bytes.fromhex(raw.decode("ascii"))
            if not raw or len(raw) % 16:
                raise ValueError("Invalid AES block")
            plain = AES.new(_EAPI_KEY, AES.MODE_ECB).decrypt(raw)
            pad = plain[-1]
            if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
                raise ValueError("Invalid padding")
            plain = plain[:-pad]
            if plain.startswith(b"\x1f\x8b"):
                plain = gzip.decompress(plain)
            value = json.loads(plain)
        except (ValueError, UnicodeDecodeError, OSError, IndexError) as error:
            raise _MusicError("网易云返回的数据无法解码。") from error
    if not isinstance(value, dict):
        raise _MusicError("网易云返回的数据格式错误。")
    return value


def _allowed_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return (
        host == "music.163.com"
        or host.endswith(".music.163.com")
        or host == "music.126.net"
        or host.endswith(".music.126.net")
    )


def _cookie_domain_allowed(domain: str) -> bool:
    domain = domain.lstrip(".").lower().rstrip(".")
    return _allowed_host(domain)


def _cookie_matches(cookie: dict[str, Any], host: str, path: str = "/") -> bool:
    domain = str(cookie.get("domain") or "music.163.com").lstrip(".").lower().rstrip(".")
    domain_match = host == domain or (not cookie.get("hostOnly", False) and host.endswith("." + domain))
    scope = str(cookie.get("path") or "/")
    return domain_match and (path == scope or (path.startswith(scope) and (scope.endswith("/") or path[len(scope):].startswith("/"))))


def _valid_expiry(value: Any) -> bool:
    if value in (None, "", 0, "0", -1, "-1"):
        return True
    try:
        return float(value) > time.time()
    except (TypeError, ValueError):
        return False


def _normal_cookie(item: dict[str, Any], default_domain: str = "music.163.com") -> dict[str, Any] | None:
    name = item.get("name")
    value = item.get("value")
    if not isinstance(name, str) or not name or not isinstance(value, str) or any(ord(char) < 32 for char in name + value) or any(char in name for char in ";= "):
        return None
    domain = str(item.get("domain") or default_domain)
    if not _cookie_domain_allowed(domain) or not _valid_expiry(
        item.get("expires", item.get("expirationDate"))
    ):
        return None
    path = str(item.get("path") or "/")
    if not path.startswith("/"):
        path = "/"
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": item.get("expires", item.get("expirationDate")),
        "secure": bool(item.get("secure", True)),
        "httpOnly": bool(item.get("httpOnly", item.get("http_only", False))),
        "hostOnly": bool(item.get("hostOnly", not domain.startswith("."))),
    }


def _parse_cookie_header(header: str) -> list[dict[str, Any]]:
    result = []
    for part in header.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        item = _normal_cookie({"name": name.strip(), "value": value, "domain": "music.163.com"})
        if item:
            result.append(item)
    return result


def _parse_cookie_file(path: Path) -> list[dict[str, Any]]:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Cookie 文件过大。")
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise ValueError("无法读取下载目录中的 cookie.txt。") from None
    stripped = text.lstrip()
    if not stripped:
        raise ValueError("Cookie 文件为空。")
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except (ValueError, TypeError):
            return []
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]
        elif isinstance(data, dict) and isinstance(data.get("cookie"), str):
            return _parse_cookie_header(data["cookie"])
        elif isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        return [cookie for item in data if isinstance(item, dict) for cookie in [_normal_cookie(item)] if cookie]
    if stripped.lower().startswith("cookie:"):
        return _parse_cookie_header(stripped.split(":", 1)[1].strip())
    if "\t" not in stripped and "=" in stripped and not stripped.startswith("#"):
        return _parse_cookie_header(stripped)

    result: list[dict[str, Any]] = []
    for line in text.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, include_subdomains, path_value, secure, expires, name, value = fields[:7]
        item = _normal_cookie(
            {
                "domain": domain,
                "path": path_value,
                "secure": secure.upper() == "TRUE",
                "expires": expires,
                "name": name,
                "value": value,
                "httpOnly": http_only,
                "hostOnly": include_subdomains.upper() != "TRUE",
            }
        )
        if item:
            result.append(item)
    return result


def _default_cookie_path() -> Path:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                for name in ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    return Path(os.path.expandvars(str(value))) / "cookie.txt"
        except (ImportError, OSError):
            pass
    return Path.home() / "Downloads" / "cookie.txt"


class MusicService:
    def __init__(self, root: Path, logger: logging.Logger, cookie_path: Path | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._logger = logger
        self._account_path = self.root / "music_account.json"
        self._cookie_source = Path(cookie_path) if cookie_path is not None else _default_cookie_path()
        self._lock = threading.RLock()
        self._client = httpx.Client(follow_redirects=False, trust_env=False, timeout=_API_TIMEOUT)
        self._cookies: list[dict[str, Any]] = []
        self._account: dict[str, Any] | None = None
        self._account_checked = False
        self._songs: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._player_status = "stopped"
        self._player_song: dict[str, Any] | None = None
        self._player_position = 0
        self._player_duration = 0
        self._player_error: str | None = None
        self._mci_alias: str | None = None
        self._audio_path: Path | None = None
        self._closed = False
        self._winmm = None
        self._load_account_locked()
        if not self._account_path.exists() and self._cookie_source.is_file():
            try:
                self._replace_cookies_locked(_parse_cookie_file(self._cookie_source), persist=True)
            except ValueError:
                self._logger.warning("music.cookie.auto_import.invalid")

    def _ensure_open_locked(self):
        if self._closed:
            raise RuntimeError("点歌服务已关闭。")

    def _load_account_locked(self) -> None:
        if not self._account_path.is_file():
            return
        try:
            data = json.loads(self._account_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if isinstance(cookies, list):
            self._cookies = [cookie for item in cookies if isinstance(item, dict) for cookie in [_normal_cookie(item)] if cookie]

    def _write_account_locked(self, cookies) -> None:
        payload = {"cookies": cookies}
        tmp = self._account_path.with_name(self._account_path.name + f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._account_path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _replace_cookies_locked(self, cookies: list[dict[str, Any]], *, persist: bool) -> None:
        unique: dict[tuple[str, str, str], dict[str, Any]] = {}
        for cookie in cookies:
            key = (cookie["domain"].lower(), cookie["path"], cookie["name"])
            unique[key] = cookie
        candidate = list(unique.values())
        if not candidate:
            raise ValueError("cookie.txt 中没有有效的网易云 Cookie，原账号信息已保留。")
        if persist:
            self._write_account_locked(candidate)
        self._cookies = candidate
        self._account = None
        self._account_checked = False

    def _cookie_header_locked(self, host: str, path: str) -> str:
        return "; ".join(
            f"{cookie['name']}={cookie['value']}"
            for cookie in self._cookies
            if _cookie_matches(cookie, host, path) and _valid_expiry(cookie.get("expires"))
        )

    def _api_request_locked(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = "https://music.163.com" + path
        body = _weapi(payload)
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://music.163.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        }
        cookie_header = self._cookie_header_locked("music.163.com", path)
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            response = self._client.post(url, data=body, headers=headers, timeout=_API_TIMEOUT)
            if not 200 <= response.status_code < 300:
                raise _MusicError(f"music service returned HTTP {response.status_code}")
            data = response.json()
        except _MusicError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise _MusicError("music service request failed") from exc
        if not isinstance(data, dict):
            raise _MusicError("music service returned invalid data")
        if data.get("code") not in (None, 0, 200):
            raise _MusicError("music service rejected request")
        return data

    def _eapi_request_locked(self, uri: str, payload: dict[str, Any]) -> dict[str, Any]:
        # eapi signs the corresponding /api path while exposing it below /eapi.
        api_uri = uri if uri.startswith("/api/") else "/api" + uri
        url = "https://interface.music.163.com/eapi" + api_uri[4:]
        header_cookie_names = {"MUSIC_U", "MUSIC_A", "NMTID", "MUSIC_R3", "WNMCID"}
        eapi_header = {
            cookie["name"]: cookie["value"]
            for cookie in self._cookies
            if cookie["name"] in header_cookie_names and _cookie_matches(cookie, "interface.music.163.com", urlparse(url).path) and _valid_expiry(cookie.get("expires"))
        }
        body = _eapi(api_uri, {**payload, "e_r": True, "header": eapi_header})
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Referer": "https://music.163.com/"}
        cookie_header = self._cookie_header_locked("interface.music.163.com", urlparse(url).path)
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            response = self._client.post(url, data=body, headers=headers, timeout=_API_TIMEOUT)
            if not 200 <= response.status_code < 300:
                raise _MusicError(f"music service returned HTTP {response.status_code}")
            return _eapi_json(response.content)
        except _MusicError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise _MusicError("music service request failed") from exc

    @staticmethod
    def _song(raw: dict[str, Any]) -> dict[str, Any] | None:
        try:
            song_id = int(raw.get("id"))
        except (TypeError, ValueError):
            return None
        if song_id <= 0:
            return None
        artists = raw.get("ar") or raw.get("artists") or []
        if isinstance(artists, list):
            artist_names = [str(item.get("name")) for item in artists if isinstance(item, dict) and item.get("name")]
        else:
            artist_names = []
        album = raw.get("al") or raw.get("album") or {}
        if not isinstance(album, dict):
            album = {}
        try:
            duration = int(raw.get("dt", raw.get("duration", 0)) or 0)
        except (TypeError, ValueError):
            duration = 0
        return {
            "id": song_id,
            "title": str(raw.get("name") or raw.get("title") or ""),
            "artists": ", ".join(artist_names),
            "album": str(album.get("name") or ""),
            "duration_ms": max(duration, 0),
        }

    def _cache_song_locked(self, song: dict[str, Any]) -> None:
        song_id = int(song["id"])
        self._songs.pop(song_id, None)
        self._songs[song_id] = song
        while len(self._songs) > _MAX_CACHE:
            self._songs.popitem(last=False)

    def _csrf_locked(self) -> str:
        for cookie in self._cookies:
            if cookie["name"] in {"__csrf", "csrfToken"}:
                return cookie["value"]
        return ""

    def _account_info_locked(self) -> None:
        if self._account_checked or not self._cookies:
            return
        self._account_checked = True
        try:
            data = self._api_request_locked("/weapi/nuser/account/get", {"csrf_token": self._csrf_locked()})
            account = data.get("account")
            profile = data.get("profile") or {}
            if isinstance(account, dict) and account.get("id") is not None:
                self._account = {
                    "id": int(account["id"]),
                    "nickname": str(profile.get("nickname") or account.get("userName") or ""),
                }
        except (ValueError, TypeError, _MusicError):
            self._account = None

    def _state_locked(self) -> dict[str, Any]:
        self._refresh_player_locked()
        self._account_info_locked()
        return {
            "credentials_loaded": bool(self._cookies),
            "cookie_count": len(self._cookies),
            "account": self._account,
            "player": {
                "status": self._player_status,
                "song": self._player_song,
                "position_ms": max(self._player_position, 0),
                "duration_ms": max(self._player_duration, 0),
                "error": self._player_error,
            },
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open_locked()
            return self._state_locked()

    def import_credentials(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open_locked()
            self._replace_cookies_locked(_parse_cookie_file(self._cookie_source), persist=True)
            return self._state_locked()

    def search(self, q: str, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        if not isinstance(q, str) or not q.strip():
            raise ValueError("请输入歌曲或歌手。")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be non-negative")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock:
            self._ensure_open_locked()
            try:
                data = self._api_request_locked(
                    "/weapi/search/get",
                    {"s": q, "type": 1, "offset": offset, "total": True, "limit": limit, "csrf_token": self._csrf_locked()},
                )
                result = data.get("result") or {}
                raw_songs = result.get("songs") if isinstance(result, dict) else []
                songs = []
                if isinstance(raw_songs, list):
                    for raw in raw_songs:
                        if isinstance(raw, dict):
                            song = self._song(raw)
                            if song:
                                self._cache_song_locked(song)
                                songs.append(song)
                try:
                    total = max(int(result.get("songCount", len(songs))), 0)
                except (TypeError, ValueError):
                    total = len(songs)
                return {"songs": songs, "total": total, "offset": offset, "limit": limit}
            except _MusicError as exc:
                raise RuntimeError("music search unavailable") from exc

    def describe(self, song_id: int) -> dict[str, Any]:
        if isinstance(song_id, bool) or not isinstance(song_id, int) or song_id <= 0:
            raise ValueError("song id must be positive")
        with self._lock:
            self._ensure_open_locked()
            cached = self._songs.get(song_id)
            if cached is not None:
                return dict(cached)
            try:
                data = self._api_request_locked(
                    "/weapi/v3/song/detail",
                    {"c": json.dumps([{"id": song_id}], separators=(",", ":")), "ids": f"[{song_id}]", "csrf_token": self._csrf_locked()},
                )
            except _MusicError as exc:
                raise RuntimeError("song details unavailable") from exc
            raw_songs = data.get("songs")
            if not isinstance(raw_songs, list) or not raw_songs or not isinstance(raw_songs[0], dict):
                raise RuntimeError("song details unavailable")
            song = self._song(raw_songs[0])
            if song is None:
                raise RuntimeError("song details unavailable")
            self._cache_song_locked(song)
            return dict(song)

    def _resolve_url_locked(self, song_id: int) -> str:
        payload = {"ids": f"[{song_id}]", "br": 320000, "csrf_token": self._csrf_locked()}
        responses: list[dict[str, Any]] = []
        for path in ("/weapi/song/enhance/player/url", "/weapi/song/enhance/player/url/v1"):
            try:
                responses.append(self._api_request_locked(path, payload))
            except _MusicError:
                continue
            data = responses[-1].get("data")
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]:
                        parsed = urlparse(item["url"])
                        if parsed.scheme in {"http", "https"} and parsed.hostname and _allowed_host(parsed.hostname):
                            return item["url"]
        try:
            data = self._eapi_request_locked(
                "/api/song/enhance/player/url/v1",
                {"ids": f"[{song_id}]", "level": "exhigh", "encodeType": "mp3"},
            )
            rows = data.get("data")
            if isinstance(rows, list):
                for item in rows:
                    if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]:
                        parsed = urlparse(item["url"])
                        if parsed.scheme in {"http", "https"} and parsed.hostname and _allowed_host(parsed.hostname):
                            return item["url"]
        except _MusicError:
            pass
        raise _MusicError("song is unavailable or requires a paid account")

    def _download_locked(self, url: str, cancel_event: threading.Event | None = None) -> Path:
        current = url
        cache_dir = self.root / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _DOWNLOAD_TIMEOUT
        # A separate cookie jar prevents API Set-Cookie values reaching CDN requests.
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=_DOWNLOAD_TIMEOUT) as downloader:
            for _ in range(5):
                if cancel_event is not None and cancel_event.is_set():
                    raise _PlaybackCancelled()
                parsed = urlparse(current)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or not _allowed_host(parsed.hostname) or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
                    raise _MusicError("音乐下载地址不受允许。")
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _MusicError("音乐下载超时。")
                    with downloader.stream("GET", current, timeout=remaining) as stream:
                        if 300 <= stream.status_code < 400:
                            location = stream.headers.get("location")
                            if not location:
                                raise _MusicError("音乐下载重定向无效。")
                            current = urljoin(current, location)
                            continue
                        if not 200 <= stream.status_code < 300:
                            raise _MusicError(f"音乐下载失败（HTTP {stream.status_code}）。")
                        try:
                            length = int(stream.headers.get("content-length", "0"))
                        except ValueError:
                            length = 0
                        if length > _MAX_DOWNLOAD:
                            raise _MusicError("音乐文件超过 50 MiB。")
                        handle = tempfile.NamedTemporaryFile(prefix="music-", suffix=".mp3", dir=cache_dir, delete=False)
                        path = Path(handle.name)
                        total = 0
                        try:
                            with handle:
                                for chunk in stream.iter_bytes(64 * 1024):
                                    if cancel_event is not None and cancel_event.is_set():
                                        raise _PlaybackCancelled()
                                    total += len(chunk)
                                    if total > _MAX_DOWNLOAD or time.monotonic() > deadline:
                                        raise _MusicError("音乐下载超时或文件超过 50 MiB。")
                                    handle.write(chunk)
                            if not total:
                                raise _MusicError("音乐文件为空。")
                            return path
                        except BaseException:
                            path.unlink(missing_ok=True)
                            raise
                except httpx.HTTPError as error:
                    raise _MusicError("音乐下载失败，请稍后重试。") from error
        raise _MusicError("音乐下载重定向次数过多。")

    def _mci_send_locked(self, command: str, result_size: int = 256) -> str:
        if sys.platform != "win32":
            raise _MusicError("本机音乐播放仅支持 Windows。")
        if self._winmm is None:
            self._winmm = ctypes.WinDLL("winmm")
            self._winmm.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
            self._winmm.mciSendStringW.restype = ctypes.c_uint
            self._winmm.mciGetErrorStringW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
            self._winmm.mciGetErrorStringW.restype = ctypes.c_bool
        output = ctypes.create_unicode_buffer(result_size)
        code = self._winmm.mciSendStringW(command, output, result_size, None)
        if code:
            message = ctypes.create_unicode_buffer(512)
            self._winmm.mciGetErrorStringW(code, message, len(message))
            raise _MusicError(f"Windows 音频错误 {code}：{message.value}")
        return output.value

    def _close_audio_locked(self) -> None:
        if self._mci_alias:
            for verb in ("stop", "close"):
                try:
                    self._mci_send_locked(f"{verb} {self._mci_alias}")
                except _MusicError:
                    self._logger.warning("music.audio.cleanup.failed operation=%s", verb)
            self._mci_alias = None
        if self._audio_path:
            try:
                self._audio_path.unlink()
            except OSError:
                pass
            self._audio_path = None

    def _refresh_player_locked(self) -> None:
        if not self._mci_alias:
            return
        try:
            mode = self._mci_send_locked(f"status {self._mci_alias} mode")
            position = self._mci_send_locked(f"status {self._mci_alias} position")
        except _MusicError as error:
            self._player_status = "error"
            self._player_error = str(error)
            self._close_audio_locked()
            return
        try:
            self._player_position = int(position or self._player_position)
        except ValueError:
            pass
        if mode.lower() == "playing":
            self._player_status = "playing"
        elif mode.lower() in {"stopped", "not ready"}:
            self._player_position = self._player_duration
            self._player_status = "stopped"
            self._close_audio_locked()
            self._player_song = None
            self._player_duration = 0

    def play(self, song: dict[str, Any], *, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        if not isinstance(song, dict) or isinstance(song.get("id"), bool):
            raise ValueError("song is invalid")
        try:
            song_id = int(song["id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("song is invalid") from None
        if song_id <= 0:
            raise ValueError("song is invalid")
        with self._lock:
            self._ensure_open_locked()
            self._close_audio_locked()
            self._player_status = "stopped"
            self._player_song = None
            self._player_position = 0
            self._player_duration = 0
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise _PlaybackCancelled()
                current = dict(self._songs.get(song_id) or self.describe(song_id))
                url = self._resolve_url_locked(song_id)
                if cancel_event is not None and cancel_event.is_set():
                    raise _PlaybackCancelled()
                path = self._download_locked(url, cancel_event)
                self._audio_path = path
                if cancel_event is not None and cancel_event.is_set():
                    raise _PlaybackCancelled()
                alias = f"remote_music_{os.getpid()}_{id(self):x}"
                self._mci_send_locked(f'open "{path}" type mpegvideo alias {alias}')
                self._mci_alias = alias
                self._mci_send_locked(f"set {alias} time format milliseconds")
                if cancel_event is not None and cancel_event.is_set():
                    raise _PlaybackCancelled()
                self._mci_send_locked(f"play {alias}")
                duration_text = self._mci_send_locked(f"status {alias} length")
                try:
                    duration = int(duration_text or 0)
                except ValueError:
                    duration = 0
                self._audio_path = path
                self._player_song = dict(current)
                self._player_duration = duration or int(current.get("duration_ms", 0) or 0)
                self._player_status = "playing"
                self._player_error = None
            except _PlaybackCancelled:
                self._close_audio_locked()
                self._player_status = "stopped"
                self._player_error = None
            except (RuntimeError, _MusicError, OSError, ValueError) as exc:
                self._close_audio_locked()
                self._player_status = "error"
                self._player_error = str(exc) if isinstance(exc, _MusicError) else "music playback failed"
            return self._state_locked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open_locked()
            self._close_audio_locked()
            self._player_status = "stopped"
            self._player_song = None
            self._player_position = 0
            self._player_duration = 0
            self._player_error = None
            return self._state_locked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._close_audio_locked()
            self._closed = True
            self._client.close()
