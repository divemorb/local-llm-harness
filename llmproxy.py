"""The sandbox's only way out: a unix-socket proxy to llama-server.

Runs inside the watchdog process (a thread). It passes only
``POST /v1/chat/completions`` and ``GET /v1/models`` to llama-server, adds
the API key itself (the sandbox never sees it), replaces image parts with a
text note (Qwen runs text-only, see ``strip_images``), and records one JSON line
per request: status, finish_reason, token usage and llama-server timings.
With a plan's ``reasoning_budget`` it caps Qwen's thinking per request, and
with ``drop_old_thinking`` it leaves earlier replies' thinking out of the
history (see ``shape_thinking``); other clients that talk to llama-server
directly keep its defaults.
It also writes what Qwen streams (thinking, answer text, tool-call
arguments) to ``llm-live.txt`` next to that log, as it arrives, for a
read-only live view (``watch.py``).
The watchdog reads the live counters for stall detection:

* ``last_activity``: monotonic time of the last byte streamed (a long
  generation counts as progress, an idle GPU doesn't)
* ``in_flight``: requests currently running
* ``events``: finished requests (``finish_reason == "length"`` or a 500
  "Failed to parse tool call arguments" mean the attempt is stuck)
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import pathlib
import re
import socketserver
import threading
import time

ALLOWED = {("POST", "/v1/chat/completions"), ("GET", "/v1/models")}
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length",
               "authorization", "host", "proxy-authorization", "upgrade", "te"}

# llama-server runs Qwen without an mmproj: one image part in the history makes every
# request a 500 ("image input is not supported") and opencode gives up (observed: Qwen
# read its own PNG screenshots). The proxy swaps such parts for this note instead.
IMAGE_PARTS = {"image_url", "image", "input_image"}
IMAGE_NOTE = ("[image removed by the watchdog: this model reads text only, so the image "
              "can't be shown. Don't open image files (PNG/JPG screenshots); the checks' "
              "text output is your view of the result.]")


def strip_images(req: dict) -> int:
    """Replace image content parts in ``req["messages"]`` with IMAGE_NOTE, in place.
    Returns how many were replaced."""
    n = 0
    for msg in req.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for i, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") in IMAGE_PARTS:
                content[i] = {"type": "text", "text": IMAGE_NOTE}
                n += 1
    return n


# Budget test: without a cap Qwen drafted whole files in its thinking until the 16k output
# limit (one step failed 3 of 3); with 4096 it wrote, tested and fixed (passed at once).
# llama-server takes the budget per request, so the server stays as it is for other clients.
# watch.py looks for "thinking budget is used up".
BUDGET_MESSAGE = ("\n\nThe thinking budget is used up. Stop planning now and act: make the next "
                  "tool call (write or edit a file, or run a check).\n")
REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text", "reasoning_details")
THINK_TAGS = re.compile(r"<think>.*?</think>\s*", re.S)


def shape_thinking(req: dict, budget: int | None, drop_old: bool) -> int:
    """Set the thinking budget on ``req`` and, with ``drop_old``, remove the thinking of
    every assistant message but the last one, in place. llama-server keeps the whole
    history's thinking (``--reasoning-preserve``), so each 4-8k-token thought stayed in the
    context and opencode summarised the session every few minutes (8192 run: 15 times in
    one attempt). Returns how many characters of thinking were removed."""
    if budget:
        req.setdefault("reasoning_budget_tokens", budget)
        req.setdefault("reasoning_budget_message", BUDGET_MESSAGE)
    if not drop_old:
        return 0
    msgs = [m for m in req.get("messages") or [] if isinstance(m, dict) and m.get("role") == "assistant"]
    removed = 0
    for msg in msgs[:-1]:
        for key in REASONING_KEYS:  # opencode sends the thinking back as reasoning_content
            if key in msg:
                value = msg.pop(key)
                removed += len(value) if isinstance(value, str) else len(json.dumps(value))
        content = msg.get("content")
        if isinstance(content, str) and "<think>" in content:
            msg["content"] = THINK_TAGS.sub("", content)
            removed += len(content) - len(msg["content"])
        elif isinstance(content, list):
            keep = [c for c in content if not (isinstance(c, dict) and c.get("type") in ("reasoning", "thinking"))]
            removed += sum(len(json.dumps(c)) for c in content if c not in keep)
            msg["content"] = keep
    return removed


class Stats:
    def __init__(self, log_path: pathlib.Path):
        self.lock = threading.Lock()
        self.log = open(log_path, "a")
        self.live = open(log_path.with_name("llm-live.txt"), "a")
        self.last_activity = time.monotonic()
        self.in_flight = 0
        self.events: list[dict] = []

    def touch(self):
        self.last_activity = time.monotonic()

    def record(self, ev: dict):
        with self.lock:
            self.events.append(ev)
            self.log.write(json.dumps(ev) + "\n")
            self.log.flush()

    def stream(self, text: str):
        with self.lock:
            self.live.write(text)
            self.live.flush()

    def since(self, index: int) -> list[dict]:
        with self.lock:
            return list(self.events[index:])

    def summary(self, since: int = 0) -> dict:
        evs = self.since(since)
        ok = [e for e in evs if e.get("status") == 200]
        return {
            "requests": len(evs),
            "http_errors": sum(1 for e in evs if e.get("status") != 200),
            "prompt_tokens": sum(e.get("prompt_tokens", 0) for e in ok),
            "prompt_evaluated": sum(e.get("prompt_n", 0) for e in ok),
            "completion_tokens": sum(e.get("completion_tokens", 0) for e in ok),
            "max_completion": max((e.get("completion_tokens", 0) for e in ok), default=0),
            "llm_seconds": round(sum(e.get("seconds", 0) for e in evs), 1),
            "prompt_seconds": round(sum(e.get("prompt_ms", 0) for e in ok) / 1000, 1),
            "length_stops": sum(1 for e in ok if e.get("finish_reason") == "length"),
            "length_stop_context": max((e.get("prompt_tokens", 0) + e.get("completion_tokens", 0)
                                        for e in ok if e.get("finish_reason") == "length"), default=0),
        }


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _make_handler(stats: Stats, upstream: tuple[str, int], api_key: str,
                  budget: int | None = None, drop_old_thinking: bool = False):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # close-delimited responses, no re-chunking

        def address_string(self):
            return "sandbox"

        def log_message(self, *args):
            pass

        def _refuse(self, code, msg):
            body = json.dumps({"error": msg}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            stats.record({"time": time.strftime("%H:%M:%S"), "path": self.path,
                          "status": code, "refused": msg})

        def _forward(self):
            path = self.path.split("?", 1)[0]
            if (self.command, path) not in ALLOWED:
                return self._refuse(403, f"{self.command} {path} is not allowed")
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            headers["Authorization"] = f"Bearer {api_key}"
            ev = {"time": time.strftime("%H:%M:%S"), "path": path}
            try:
                req = json.loads(body) if body else {}
                ev["messages"] = len(req.get("messages", []))
                ev["max_tokens"] = req.get("max_tokens") or req.get("max_completion_tokens")
                changed = False
                if removed := strip_images(req):
                    ev["images_removed"] = removed
                    changed = True
                if path.endswith("completions") and (budget or drop_old_thinking):
                    if dropped := shape_thinking(req, budget, drop_old_thinking):
                        ev["thinking_dropped"] = dropped
                    changed = True
                if changed:
                    body = json.dumps(req).encode()
            except (ValueError, AttributeError):
                pass
            start = time.monotonic()
            live = _Live(stats) if path.endswith("completions") else None
            if live:
                live.header(len(stats.events) + 1, ev)
            with stats.lock:
                stats.in_flight += 1
            stats.touch()
            try:
                conn = http.client.HTTPConnection(*upstream, timeout=3600)
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                ev["status"] = resp.status
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP_HEADERS:
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                tail, err_body = b"", b""
                while chunk := resp.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    stats.touch()
                    tail = _scan(tail + chunk, ev, live)
                    if resp.status != 200 and len(err_body) < 4000:
                        err_body += chunk
                if resp.status != 200:
                    ev["error"] = err_body[:1000].decode("utf-8", "replace")
                elif not ev.get("finish_reason") and path.endswith("completions"):
                    _scan(tail + b"\n", ev, live)
                    _from_json(tail, ev)
                conn.close()
            except (OSError, http.client.HTTPException) as exc:
                ev.setdefault("status", 502)
                ev["error"] = repr(exc)
            finally:
                with stats.lock:
                    stats.in_flight -= 1
                ev["seconds"] = round(time.monotonic() - start, 1)
                stats.touch()
                stats.record(ev)

        do_GET = _forward
        do_POST = _forward

        def do_PUT(self):
            self._refuse(403, "method not allowed")

        do_DELETE = do_PATCH = do_PUT

    return Handler


class _Live:
    """One request's streamed output, labelled when the kind changes."""

    def __init__(self, stats: Stats):
        self.stats, self.kind = stats, None

    def header(self, n: int, ev: dict):
        self.stats.stream(f"\n\n════ request {n} · {ev['time']} · {ev.get('messages')} messages ════\n")

    def put(self, kind: str, text: str):
        if not text:
            return
        if kind != self.kind:
            self.kind = kind
            self.stats.stream(f"\n── {kind} ──\n")
        self.stats.stream(text)

    def delta(self, delta: dict):
        self.put("thinking", delta.get("reasoning_content") or delta.get("reasoning") or "")
        self.put("answer", delta.get("content") or "")
        for call in delta.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name"):
                self.kind = None
                self.put(f"tool call: {fn['name']}", " ")
            self.put(self.kind or "tool call", fn.get("arguments") or "")

    def finish(self, reason: str):
        self.stats.stream(f"\n── finished: {reason} ──\n")
        self.kind = None


def _apply(obj: dict, ev: dict, live: _Live | None = None):
    for choice in obj.get("choices") or []:
        if live:
            live.delta(choice.get("delta") or {})
        if choice.get("finish_reason"):
            ev["finish_reason"] = choice["finish_reason"]
            if live:
                live.finish(choice["finish_reason"])
    usage = obj.get("usage") or {}
    for k in ("prompt_tokens", "completion_tokens"):
        if k in usage:
            ev[k] = usage[k]
    timings = obj.get("timings") or {}
    for k in ("prompt_n", "cache_n", "prompt_ms", "predicted_ms"):
        if k in timings:
            ev[k] = round(timings[k], 1) if isinstance(timings[k], float) else timings[k]


def _scan(buf: bytes, ev: dict, live: _Live | None = None) -> bytes:
    """Parse complete SSE ``data:`` lines; return the incomplete rest."""
    *lines, rest = buf.split(b"\n")
    for line in lines:
        line = line.strip()
        if line.startswith(b"data:") and line != b"data: [DONE]":
            try:
                _apply(json.loads(line[5:]), ev, live)
            except ValueError:
                pass
    return rest[-200_000:]


def _from_json(buf: bytes, ev: dict):
    """Non-streaming response: the whole body is one JSON object."""
    try:
        _apply(json.loads(buf), ev)
    except ValueError:
        pass


def start(sock_path: pathlib.Path, log_path: pathlib.Path, api_key: str,
          upstream: tuple[str, int] = ("127.0.0.1", 8080), budget: int | None = None,
          drop_old_thinking: bool = False) -> tuple[Stats, _Server]:
    if sock_path.exists():
        sock_path.unlink()
    stats = Stats(log_path)
    server = _Server(str(sock_path), _make_handler(stats, upstream, api_key, budget, drop_old_thinking))
    os.chmod(sock_path, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return stats, server
