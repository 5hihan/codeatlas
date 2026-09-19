"""Keeps the index up to date automatically.

Two mechanisms, both always on while a server runs:

* a filesystem watcher (``watchdog``) that debounces change events and
  re-indexes only the touched files, typically within a second of a save;
* a periodic full stat-scan (cheap: it only compares size/mtime) as a safety
  net for editors/tools that bypass change notifications, git checkouts that
  swap thousands of files at once, and network drives.

Every MCP tool call additionally triggers a stat-scan when the last one is
older than a few seconds, so an agent never reads stale data even if the
watcher thread died.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .indexer import DEFAULT_IGNORE_DIRS, Index
from .languages import lang_for_path
from .relationships import is_artifact_path

log = logging.getLogger("codeatlas.watcher")


class Watcher:
    def __init__(self, index: Index, debounce: float = 0.6, rescan_every: float = 120.0,
                 on_change: Optional[Callable[[dict], None]] = None):
        self.ix = index
        self.debounce = debounce
        self.rescan_every = rescan_every
        self.on_change = on_change
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer = None
        self._thread: Optional[threading.Thread] = None
        self.events = 0
        self.last_event: float = 0.0
        self.backend = "polling"

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "Watcher":
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            watcher = self

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if event.event_type not in ("created", "modified", "moved", "deleted"):
                        return
                    if event.is_directory and event.event_type not in ("moved", "deleted"):
                        return
                    paths = [getattr(event, "src_path", None), getattr(event, "dest_path", None)]
                    for p in paths:
                        if p:
                            watcher._queue(p, is_directory=event.is_directory)

            self._observer = Observer()
            self._observer.schedule(Handler(), str(self.ix.repo), recursive=True)
            self._observer.daemon = True
            self._observer.start()
            self.backend = "watchdog"
        except Exception as e:  # pragma: no cover - platform specific
            log.warning("filesystem watcher unavailable (%s); falling back to polling", e)
            self._observer = None
            self.backend = "polling"
            self.rescan_every = min(self.rescan_every, 10.0)
        self._thread = threading.Thread(target=self._loop, name="codeatlas-watcher", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass

    # ------------------------------------------------------------------ internals
    def _queue(self, path: str, is_directory: bool = False) -> None:
        p = str(path)
        rel = os.path.relpath(p, str(self.ix.repo)).replace("\\", "/")
        if rel == ".codeatlas/ignore":
            with self._lock:
                self._pending.add("*")
                self.last_event = time.time()
            return
        if rel.startswith("..") or rel.startswith(".codeatlas") or rel in ("SCRUM.md", "SNIPE.md"):
            return
        parts = rel.split("/")
        if any(part in DEFAULT_IGNORE_DIRS or part.startswith(".git") for part in parts[:-1]):
            return
        is_directory = is_directory or os.path.isdir(p)
        if lang_for_path(rel) is None and not is_artifact_path(rel) and not is_directory:
            # a directory move/delete may drop many files; mark full rescan
            if rel.endswith((".gitignore",)):
                with self._lock:
                    self._pending.add("*")
            return
        with self._lock:
            self._pending.add("*" if is_directory else rel)
            self.events += 1
            self.last_event = time.time()

    def _loop(self) -> None:
        last_full = time.time()
        while not self._stop.is_set():
            time.sleep(0.25)
            now = time.time()
            batch: Optional[set[str]] = None
            with self._lock:
                if self._pending and now - self.last_event >= self.debounce:
                    batch = set(self._pending)
                    self._pending.clear()
            try:
                if batch is not None:
                    if "*" in batch:
                        stats = self.ix.update()
                    else:
                        stats = self.ix.update(paths=sorted(batch))
                    self._notify(stats.as_dict(), "watch")
                elif now - last_full >= self.rescan_every:
                    stats = self.ix.update()
                    last_full = now
                    if stats.parsed or stats.removed or stats.relationship_files:
                        self._notify(stats.as_dict(), "rescan")
            except Exception as e:  # pragma: no cover
                log.exception("watcher update failed: %s", e)
                time.sleep(2)

    def _notify(self, stats: dict, source: str) -> None:
        if self.on_change and (stats.get("parsed") or stats.get("removed") or stats.get("relationship_files")):
            try:
                self.on_change({"source": source, **stats})
            except Exception:
                pass

    def status(self) -> dict:
        return {"backend": self.backend, "events": self.events, "pending": len(self._pending),
                "last_event": self.last_event, "rescan_every": self.rescan_every,
                "alive": bool(self._thread and self._thread.is_alive())}
