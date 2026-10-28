"""Server-enforced, session-bound, single-use music playback confirmation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import secrets
import threading
import time


@dataclass
class PendingSong:
    owner: str
    song: dict
    started: float
    created_at: datetime
    wait: int


class MusicConfirmation:
    def __init__(self, music, *, clock=None, monotonic=None):
        self.music = music
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.monotonic = monotonic or time.monotonic
        self.lock = threading.Lock()
        self.playback_lock = threading.Lock()
        self.pending: dict[str, PendingSong] = {}
        self.active = None
        self.last_played = None
        self.closed = False

    @staticmethod
    def required_wait(now):
        return 50 if now.hour < 21 else 5

    def time_state(self):
        now = self.clock()
        return {"local_time": now.isoformat(timespec="seconds"), "timezone": now.tzname() or str(now.utcoffset()), "confirmation_seconds": self.required_wait(now)}

    def _purge(self):
        current = self.monotonic()
        expired = [ticket for ticket, item in self.pending.items() if current - item.started >= 300]
        for ticket in expired:
            del self.pending[ticket]

    def _item(self, owner, ticket):
        self._purge()
        item = self.pending.get(ticket)
        if item is None or not secrets.compare_digest(item.owner, owner):
            raise ValueError("点歌确认已取消或过期，请重新选择歌曲。")
        # Crossing midnight cannot preserve an earlier, shorter confirmation window.
        item.wait = max(item.wait, self.required_wait(self.clock()))
        return item

    def _status(self, ticket, item):
        elapsed = self.monotonic() - item.started
        return {"ticket": ticket, "song": dict(item.song), "wait_seconds": item.wait, "remaining_seconds": max(0, math.ceil(item.wait - elapsed)), "ready_at": (item.created_at + timedelta(seconds=item.wait)).isoformat(timespec="seconds"), "expires_in": max(0, math.ceil(300 - elapsed)), **self.time_state()}

    def prepare(self, owner, song_id):
        # Fetch metadata only; resolve/download/play audio strictly after confirmation.
        song = self.music.describe(song_id)
        with self.lock:
            if self.closed:
                raise RuntimeError("点歌服务已关闭。")
            self._purge()
            for key in [key for key, item in self.pending.items() if item.owner == owner]:
                del self.pending[key]
            if len(self.pending) >= 64:
                raise RuntimeError("等待确认的点歌过多，请稍后重试。")
            now = self.clock()
            item = PendingSong(owner, dict(song), self.monotonic(), now, self.required_wait(now))
            ticket = secrets.token_urlsafe(32)
            self.pending[ticket] = item
            return self._status(ticket, item)

    def status(self, owner, ticket):
        with self.lock:
            return self._status(ticket, self._item(owner, ticket))

    def confirm(self, owner, ticket):
        with self.playback_lock:
            with self.lock:
                if self.closed:
                    raise RuntimeError("点歌服务已关闭。")
                item = self._item(owner, ticket)
                remaining = self._status(ticket, item)["remaining_seconds"]
                if remaining:
                    raise RuntimeError(f"仍需等待 {remaining} 秒后确认播放。")
                # Consume before any download/IO so concurrent submissions cannot replay.
                del self.pending[ticket]
                song = dict(item.song)
                cancelled = threading.Event()
                self.active = (owner, ticket, cancelled)
                self.last_played = (owner, ticket)
            try:
                return self.music.play(song, cancel_event=cancelled)
            finally:
                with self.lock:
                    self.active = None

    def cancel(self, owner, ticket=None):
        with self.lock:
            if ticket is not None:
                item = self.pending.get(ticket)
                if item is not None and secrets.compare_digest(item.owner, owner):
                    del self.pending[ticket]
            else:
                for key in [key for key, item in self.pending.items() if item.owner == owner]:
                    del self.pending[key]
            target = self.last_played if ticket is not None and self.last_played == (owner, ticket) else None
            if self.active is not None and self.active[0] == owner and (ticket is None or ticket == self.active[1]):
                self.active[2].set()
                target = self.active[:2]
        if target is not None:
            with self.playback_lock:
                if self.last_played == target:
                    self.music.stop()
        return {"ok": True}

    def stop(self, owner):
        with self.lock:
            if self.active is not None:
                self.active[2].set()
        with self.playback_lock:
            self.cancel(owner)
            return self.music.stop()

    def close(self):
        with self.lock:
            self.closed = True
            self.pending.clear()
            if self.active is not None:
                self.active[2].set()
        with self.playback_lock:
            pass  # Drain any operation that started before shutdown.
