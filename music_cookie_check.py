"""Check credential-import scope and failed-import preservation with synthetic cookies."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile

from music_service import MusicService, _cookie_matches, _parse_cookie_file


def check():
    with tempfile.TemporaryDirectory(prefix="music-cookie-check-") as directory:
        root = Path(directory)
        source = root / "cookie.txt"
        source.write_text("# Netscape HTTP Cookie File\n#HttpOnly_music.163.com\tFALSE\t/weapi/search\tTRUE\t0\tMUSIC_U\ttest-host-token\n.music.163.com\tTRUE\t/\tTRUE\t0\t__csrf\ttest-csrf\nattacker.example\tTRUE\t/\tTRUE\t0\tforeign\tsecret\n.music.163.com\tTRUE\t/\tTRUE\t1\texpired\told\n", encoding="utf-8")
        cookies = _parse_cookie_file(source)
        assert len(cookies) == 2
        host_cookie = next(cookie for cookie in cookies if cookie["name"] == "MUSIC_U")
        assert host_cookie["hostOnly"] and host_cookie["httpOnly"]
        assert _cookie_matches(host_cookie, "music.163.com", "/weapi/search/get")
        assert not _cookie_matches(host_cookie, "music.163.com", "/weapi/searching")
        assert not _cookie_matches(host_cookie, "music.163.com", "/weapi/nuser/account/get")
        assert not _cookie_matches(host_cookie, "interface.music.163.com", "/weapi/search/get")
        csrf = next(cookie for cookie in cookies if cookie["name"] == "__csrf")
        assert _cookie_matches(csrf, "interface.music.163.com", "/eapi/song")
        service = MusicService(root, logging.getLogger("cookie-check"), cookie_path=source)
        try:
            original = (root / "music_account.json").read_bytes()
            source.write_text("not a valid Cookie", encoding="utf-8")
            try:
                service.import_credentials()
                raise AssertionError("Invalid credentials must not replace an existing account")
            except ValueError:
                pass
            assert (root / "music_account.json").read_bytes() == original
            assert "MUSIC_U=" in service._cookie_header_locked("music.163.com", "/weapi/search/get")
            assert "MUSIC_U" not in service._cookie_header_locked("interface.music.163.com", "/eapi/song")
        finally:
            service.close()
        source.write_text("Cookie: MUSIC_U=header-test; __csrf=csrf-test", encoding="utf-8")
        assert len(_parse_cookie_file(source)) == 2
        source.write_text(json.dumps([{"domain": "music.163.com", "name": "MUSIC_U", "value": "json-test", "hostOnly": True, "expirationDate": -1}]), encoding="utf-8")
        assert _parse_cookie_file(source)[0]["hostOnly"]
    print("PASS: Netscape/HttpOnly, cookie-header and JSON imports, foreign/expired filtering, host-only/path scope, session expiry and atomic failed-import preservation. Credentials synthetic.")


if __name__ == "__main__":
    check()
