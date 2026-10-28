"""Check server-enforced music waiting periods without audio/network side effects."""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import threading
import logging
from pathlib import Path
import tempfile

from music_service import MusicService

from music_confirmation import MusicConfirmation


class Clock:
    def __init__(self, hour, minute=0, second=0):
        self.wall = datetime(2026, 10, 27, hour, minute, second, tzinfo=timezone(timedelta(hours=8)))
        self.elapsed = 0.0

    def advance(self, seconds):
        self.elapsed += seconds
        self.wall += timedelta(seconds=seconds)


class Player:
    def __init__(self):
        self.played = []

    def describe(self, song_id):
        return {"id": song_id, "title": "Confirmation check", "artists": "Test", "album": "", "duration_ms": 1000}

    def play(self, song, *, cancel_event=None):
        self.played.append(song["id"])
        return {"player": {"status": "playing", "song": song}}

    def stop(self):
        self.played.clear()
        return {"player": {"status": "stopped"}}


def must_reject(operation):
    try:
        operation()
    except (ValueError, RuntimeError):
        return
    raise AssertionError("Playback bypassed the required confirmation boundary")


def confirmation(clock):
    player = Player()
    return player, MusicConfirmation(player, clock=lambda: clock.wall, monotonic=lambda: clock.elapsed)


def check():
    for hour, required in ((0, 50), (20, 50), (21, 5), (23, 5)):
        clock = Clock(hour)
        player, gate = confirmation(clock)
        ticket = gate.prepare("browser-A", 1)["ticket"]
        assert gate.status("browser-A", ticket)["wait_seconds"] == required
        must_reject(lambda: gate.status("browser-B", ticket))
        must_reject(lambda: gate.confirm("browser-B", ticket))
        clock.advance(required - 0.01)
        must_reject(lambda: gate.confirm("browser-A", ticket))
        assert not player.played
        clock.advance(0.01)
        gate.confirm("browser-A", ticket)
        assert player.played == [1]
        must_reject(lambda: gate.confirm("browser-A", ticket))
    clock = Clock(20, 59, 58)
    player, gate = confirmation(clock)
    ticket = gate.prepare("A", 2)["ticket"]
    clock.advance(5)
    assert gate.status("A", ticket)["remaining_seconds"] == 45
    must_reject(lambda: gate.confirm("A", ticket))
    clock.advance(45)
    gate.confirm("A", ticket)
    clock = Clock(23, 59, 58)
    player, gate = confirmation(clock)
    ticket = gate.prepare("A", 3)["ticket"]
    clock.advance(5)
    assert gate.status("A", ticket)["remaining_seconds"] == 45
    must_reject(lambda: gate.confirm("A", ticket))
    clock.advance(45)
    gate.confirm("A", ticket)
    # Changing the wall clock cannot bypass the monotonic waiting period.
    clock = Clock(21)
    player, gate = confirmation(clock)
    ticket = gate.prepare("A", 4)["ticket"]
    clock.wall += timedelta(hours=2)
    must_reject(lambda: gate.confirm("A", ticket))
    gate.cancel("A", ticket)
    must_reject(lambda: gate.confirm("A", ticket))
    ticket = gate.prepare("A", 5)["ticket"]
    clock.advance(300)
    must_reject(lambda: gate.confirm("A", ticket))
    ticket = gate.prepare("A", 6)["ticket"]
    newer = gate.prepare("A", 7)["ticket"]
    must_reject(lambda: gate.confirm("A", ticket))
    clock.advance(5)
    def submit():
        try:
            gate.confirm("A", newer)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: submit(), range(4))) == 1
    assert player.played == [7]
    gate.close()
    must_reject(lambda: gate.prepare("A", 8))
    print("PASS: local-time 21:00 boundary, 5/50-second monotonic wait, midnight tightening, owner binding, cancel/expiry/replacement, single-use concurrency and shutdown. Audio adapter simulated.")


def check_stop_race():
    clock = Clock(21)
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

    class SlowPlayer(Player):
        def play(self, song, *, cancel_event=None):
            entered.set()
            assert release.wait(timeout=5)
            self.played.append(song["id"])
            return {"player": {"status": "playing"}}

        def stop(self):
            self.played.clear()
            stopped.set()
            return {"player": {"status": "stopped"}}

    player = SlowPlayer()
    gate = MusicConfirmation(player, clock=lambda: clock.wall, monotonic=lambda: clock.elapsed)
    ticket = gate.prepare("A", 9)["ticket"]
    clock.advance(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        playing = pool.submit(gate.confirm, "A", ticket)
        assert entered.wait(timeout=3)
        stop = pool.submit(gate.stop, "A")
        try:
            assert not stopped.wait(timeout=0.1)
        finally:
            release.set()
        playing.result(timeout=3)
        assert stop.result(timeout=3)["player"]["status"] == "stopped"
    assert not player.played
    print("PASS: a concurrent stop cannot return before an earlier confirmation playback is stopped. Audio adapter simulated.")


def check_cancel_download():
    with tempfile.TemporaryDirectory(prefix="music-cancel-check-") as directory:
        root = Path(directory)
        music = MusicService(root, logging.getLogger("cancel-check"), cookie_path=root / "cookie.txt")
        song = Player().describe(1)
        music._songs[1] = song
        music._resolve_url_locked = lambda song_id: "https://m1.music.126.net/synthetic.mp3"
        entered, release = threading.Event(), threading.Event()
        commands = []
        music._mci_send_locked = lambda command: commands.append(command) or "0"
        audio = root / "synthetic.mp3"

        def download(url, cancel_event=None):
            entered.set()
            assert release.wait(timeout=5)
            audio.write_bytes(b"synthetic audio")
            return audio

        music._download_locked = download
        clock = Clock(21)
        gate = MusicConfirmation(music, clock=lambda: clock.wall, monotonic=lambda: clock.elapsed)
        try:
            ticket = gate.prepare("A", 1)["ticket"]
            clock.advance(5)
            with ThreadPoolExecutor(max_workers=2) as pool:
                playing = pool.submit(gate.confirm, "A", ticket)
                assert entered.wait(timeout=3)
                assert gate.cancel("B", ticket)["ok"]
                assert not gate.active[2].is_set(), "Another session cancelled an in-flight confirmation"
                cancellation = pool.submit(gate.cancel, "A", ticket)
                try:
                    assert gate.active[2].wait(timeout=3)
                finally:
                    release.set()
                assert cancellation.result(timeout=3)["ok"]
                assert playing.result(timeout=3)["player"]["status"] == "stopped"
            assert not commands, "Cancellation during download must prevent native audio open/play"
            assert not audio.exists(), "Cancelled downloaded audio must be removed"
        finally:
            gate.close()
            music.close()
    player, gate = confirmation(Clock(21))
    ticket = gate.prepare("A", 1)["ticket"]
    gate.monotonic = lambda: 5
    gate.confirm("A", ticket)
    gate.cancel("B", ticket)
    assert player.played == [1]
    gate.cancel("A", ticket)
    assert not player.played, "Cancel must cover playback whose response is still in transit"
    print("PASS: cancellation during download and before confirmation response prevents late audio; another session cannot cancel it. Network/audio simulated.")


if __name__ == "__main__":
    check()
    check_stop_race()
    check_cancel_download()
