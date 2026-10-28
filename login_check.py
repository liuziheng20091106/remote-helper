"""Run the hidden-entry and per-run login limit against isolated real HTTP servers."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile

import httpx

from main import create_app
from relay_smoke import Server


def check():
    with tempfile.TemporaryDirectory(prefix="remote-helper-login-") as directory:
        root = Path(directory)
        (root / "static").mkdir()
        (root / "static/index.html").write_text("<html>Private entry</html>", encoding="utf-8")
        for restarted in (False, True):
            app = create_app(root, music_cookie_path=root / "cookie.txt")
            server = Server(app)
            server.start()
            base = f"http://127.0.0.1:{server.port}"
            try:
                with httpx.Client(base_url=base, trust_env=False, follow_redirects=False) as client:
                    for target in ("/", "/?p=114514", "/r/", "/R"):
                        response = client.get(target)
                        assert response.status_code == 404 and "location" not in response.headers
                    assert client.get("/r").status_code == 200
                    assert client.get("/api/state").status_code == 401
                    login = client.get("/r?p=114514")
                    assert login.status_code == 200 and "location" not in login.headers
                    assert client.get("/api/state").status_code == 200
                    if restarted:
                        break

                    def wrong_login(_):
                        return httpx.get(base + "/r?p=wrong", trust_env=False, timeout=10).status_code

                    with ThreadPoolExecutor(max_workers=6) as pool:
                        assert sorted(pool.map(wrong_login, range(6))) == [401, 429, 429, 429, 429, 429]
                    # The limit governs new logins, not already authenticated sessions.
                    assert client.get("/api/state").status_code == 200
                    assert client.post("/api/logout", json={}, headers={"X-Remote-Helper": "1"}).status_code == 200
                    for headers in ({}, {"REALIP": "203.0.113.31"}, {"X-Forwarded-For": "203.0.113.32"}, {"X-Real-IP": "203.0.113.33"}):
                        response = client.get("/r?p=114514", headers=headers)
                        assert response.status_code == 429
                        assert client.get("/api/state").status_code == 401
                    app.state.store.update(web_paused=True)
                    assert client.get("/").status_code == 404
                    app.state.store.update(web_paused=False)
                    assert client.get("/r?p=114514").status_code == 429
            finally:
                server.stop()
                for handler in list(app.state.logger.handlers):
                    handler.close()
                    app.state.logger.removeHandler(handler)
    print("PASS: hidden /r entry without redirects, root 404, at most one failed attempt under concurrency, direct header spoof resistance, logout/pause retain ban, restart clears ban. Real HTTP; isolated settings.")


if __name__ == "__main__":
    check()
