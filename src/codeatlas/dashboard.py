"""Localhost dashboard: a single-page web UI over the same index + SNIPE state.

Started automatically by ``codeatlas serve``; also runnable standalone with
``codeatlas dashboard``.  One dashboard per repo: if another CodeAtlas process
already serves this repo, the URL is reused instead of starting a second one.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import mimetypes
import queue

from . import __version__, changes, fmt, rank, patterns, relationships

log = logging.getLogger("codeatlas.dashboard")
DEFAULT_PORT = 8765
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class EventBus:
    """Fan-out of server events to SSE subscribers."""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


def _free_port(start: int) -> int:
    for p in range(start, start + 60):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


def _probe(url: str, repo: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/api/ping", timeout=0.6) as r:
            d = json.loads(r.read().decode())
            return d.get("repo") == repo
    except Exception:
        return False


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # ignore client disconnects (SSE, keep-alive)
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Dashboard:
    def __init__(self, app, port: Optional[int] = None):
        self.app = app
        self.port = port
        self.url = ""
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.reused = False
        self.info_path = app.ix.dir / "dashboard.json"

    def start(self) -> "Dashboard":
        repo = str(self.app.repo)
        if self.port is None and self.info_path.exists():
            try:
                info = json.loads(self.info_path.read_text(encoding="utf-8"))
                if _probe(info.get("url", ""), repo):
                    self.url = info["url"]
                    self.reused = True
                    log.info("dashboard already running for this repo at %s", self.url)
                    return self
            except Exception:
                pass
        port = self.port or _free_port(DEFAULT_PORT)
        if not port:
            raise RuntimeError("no free port for dashboard")
        handler = _make_handler(self.app)
        self.server = _QuietServer(("127.0.0.1", port), handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, name="codeatlas-dashboard", daemon=True)
        self.thread.start()
        self.info_path.write_text(json.dumps({"url": self.url, "pid": os.getpid(), "repo": repo, "started": time.time()}),
                                  encoding="utf-8")
        log.info("dashboard at %s", self.url)
        return self

    def stop(self) -> None:
        if self.server:
            try:
                self.server.shutdown()
            except Exception:
                pass
            try:
                if self.info_path.exists() and json.loads(self.info_path.read_text()).get("pid") == os.getpid():
                    self.info_path.unlink()
            except Exception:
                pass


def _make_handler(app):
    atlas = app.atlas

    class Handler(BaseHTTPRequestHandler):
        server_version = f"CodeAtlas/{__version__}"

        def log_message(self, *a):  # quiet
            pass

        # -- helpers -------------------------------------------------------------
        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _text(self, text: str, ctype: str = "text/plain; charset=utf-8") -> None:
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, path: str) -> None:
            rel = path.lstrip("/") or "index.html"
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(os.path.normpath(STATIC_DIR)) or not os.path.isfile(full):
                full = os.path.join(STATIC_DIR, "index.html")  # SPA fallback
            if not os.path.isfile(full):
                return self._text("CodeAtlas dashboard is not built. Run `npm run build` in dashboard/ "
                                  "(or reinstall the package).")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache" if rel == "index.html" else "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(body)

        def _sse(self) -> None:
            q = app.bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            nl = "\n"
            try:
                self.wfile.write(("event: hello" + nl + "data: {}" + nl + nl).encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        payload = json.dumps(ev, default=str)
                        msg = "event: " + str(ev.get("type", "message")) + nl + "data: " + payload + nl + nl
                        self.wfile.write(msg.encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write((": ping" + nl + nl).encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                app.bus.unsubscribe(q)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        # -- routes --------------------------------------------------------------
        def do_GET(self) -> None:
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = u.path
            try:
                if not path.startswith("/api/"):
                    return self._static(path)
                if path == "/api/events":
                    return self._sse()
                if path == "/api/ping":
                    return self._json({"ok": True, "repo": str(app.repo), "version": __version__})
                with app._lock:
                    if path == "/api/status":
                        s = app.status()
                        s["events"] = app.events[-40:][::-1]
                        return self._json(s)
                    if path == "/api/overview":
                        atlas.ensure_fresh()
                        return self._json(atlas.overview())
                    if path == "/api/relationships":
                        atlas.ensure_fresh()
                        return self._json(relationships.relationships(app.ix, q.get("path") or None,
                            q.get("kind") or None, q.get("direction", "both"),
                            int(q.get("limit", 50)), int(q.get("offset", 0))))
                    if path == "/api/patterns/catalogue":
                        return self._json(patterns.catalogue())
                    if path == "/api/patterns":
                        atlas.ensure_fresh()
                        return self._json(patterns.patterns(app.ix, q.get("path") or None, q.get("pattern") or None,
                            int(q.get("limit", 50)), int(q.get("offset", 0))))
                    if path == "/api/changes":
                        return self._json(changes.change_context(app.ix, atlas, q.get("base", "HEAD"),
                            q.get("untracked", "1") == "1", depth=int(q.get("depth", 3)),
                            comparison=q.get("comparison", "working"), merge_base=q.get("merge_base") == "1",
                            max_files=int(q.get("max_files", 200))))
                    if path == "/api/map":
                        budget = int(q["budget"]) if q.get("budget") else None
                        focus = [q["focus"]] if q.get("focus") else None
                        return self._text(rank.repo_map(app.ix, budget, focus_paths=focus))
                    if path == "/api/tree":
                        return self._json(atlas.file_tree(int(q.get("depth", 4))))
                    if path == "/api/files":
                        return self._json(atlas.list_files(q.get("prefix", ""), int(q.get("limit", 2000))))
                    if path == "/api/outline":
                        o = atlas.outline(q.get("path", ""))
                        return self._json(o or {"error": "not indexed"})
                    if path == "/api/search":
                        return self._json(atlas.find_symbols(q.get("q", ""), q.get("kind") or None, q.get("path") or None,
                                                             int(q.get("limit", 40))))
                    if path == "/api/symbol":
                        d = atlas.symbol_detail(q.get("name", ""), q.get("path") or None)
                        if d.get("found"):
                            d["impact"] = atlas.impact(q.get("name", ""), q.get("path") or None, depth=2)
                            d["triggers"] = atlas.triggers(q.get("name", ""), q.get("path") or None)
                            d["source"] = atlas.source(q.get("name", ""), q.get("path") or None, max_lines=200)
                        return self._json(d)
                    if path == "/api/references":
                        return self._json(atlas.references(q.get("name", ""), limit=200))
                    if path == "/api/dependencies":
                        return self._json(atlas.dependencies(q.get("path", "")) or {"error": "not indexed"})
                    if path == "/api/hotspots":
                        return self._json(atlas.hotspots(int(q.get("limit", 15))))
                    if path == "/api/modules":
                        return self._json(atlas.module_graph(int(q.get("depth", 2)), int(q.get("min", 1))))
                    if path == "/api/findings":
                        return self._json(atlas.findings(q.get("code") or None, q.get("path") or None, int(q.get("limit", 300))))
                    if path == "/api/unreferenced":
                        return self._json(atlas.unreferenced(int(q.get("limit", 100)), q.get("exported") == "1"))
                    if path == "/api/grep":
                        return self._json(atlas.grep(q.get("q", ""), q.get("glob") or None, int(q.get("limit", 200))))
                    if path == "/api/top":
                        return self._json({"symbols": rank.top_symbols(app.ix, int(q.get("limit", 40))),
                                           "files": rank.top_files(app.ix, int(q.get("limit", 40)))})
                    if path == "/api/snipe":
                        sn = app.snipe
                        b = sn.board()
                        b["watcher_alive"] = bool(app.watcher and app.watcher.status()["alive"])
                        return self._json({"board": b, "items": sn.data["items"]})
                    if path == "/api/snipe/context":
                        return self._text(app.snipe.context(q.get("id", ""), int(q.get("level", 1))))
                    if path == "/api/snipe.md":
                        return self._text(app.snipe.render_md(), "text/markdown; charset=utf-8")
                self._json({"error": "not found"}, 404)
            except Exception as e:  # pragma: no cover
                log.exception("dashboard GET %s", path)
                self._json({"error": str(e)}, 500)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            body = self._body()
            try:
                with app._lock:
                    sn = app.snipe
                    if path == "/api/snipe/scan":
                        return self._json(sn.scan(body.get("run")))
                    if path == "/api/snipe/next":
                        it = sn.next(body.get("agent"))
                        return self._json({"item": it})
                    if path == "/api/snipe/claim":
                        return self._json(sn.claim(str(body["id"]), body.get("agent")))
                    if path == "/api/snipe/verify":
                        return self._json(sn.verify(str(body["id"]), bool(body.get("deep"))))
                    if path == "/api/snipe/update":
                        return self._json(sn.update(str(body["id"]), body.get("state"), body.get("note"), body.get("priority"),
                                                    body.get("suspect"), body.get("kind"), body.get("agent")))
                    if path == "/api/snipe/abort":
                        return self._json(sn.abort(str(body["id"]), body.get("reason", "aborted from dashboard")))
                    if path == "/api/snipe/add":
                        rec = sn.add(body.get("problem", "untitled"), body.get("evidence") or [], body.get("kind", "issue"),
                                     body.get("suspect"), priority=body.get("priority"), source="manual")
                        sn.save()
                        return self._json({"ok": True, "item": rec})
                    if path == "/api/snipe/report":
                        created = sn.report(body.get("text", ""), body.get("source", "test"))
                        return self._json({"ok": True, "created": [c["id"] for c in created]})
                    if path == "/api/snipe/config":
                        return self._json(sn.set_config(**{k: v for k, v in body.items() if k in sn.data["config"]}))
                    if path == "/api/reindex":
                        st = app.ix.update(full=bool(body.get("full")))
                        rank.compute_pagerank(app.ix)
                        return self._json(st.as_dict())
                self._json({"error": "not found"}, 404)
            except Exception as e:  # pragma: no cover
                log.exception("dashboard POST %s", path)
                self._json({"error": str(e)}, 500)

    return Handler
