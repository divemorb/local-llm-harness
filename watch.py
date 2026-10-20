#!/usr/bin/env python3
"""Watch Qwen work, live and read-only, in a second terminal.

Follows the newest watchdog run (or the run folder given) and shows which
phase the step is in, derived from what Qwen does rather than what it thinks:

    ■ S2, attempt 3 (18:55)
    [18:55] 1 understanding the task
    [18:57] 2 reading  · src/app/views.py (100-160)
    [19:01] 3 writing  · write src/app/report.py (170 lines)
    [19:03] 4 testing  · pytest tests/test_report.py
             ↳ 41 passed, 2 failed
    [19:10] 5 fixing   · edit src/app/report.py
      … thinking 3.1k tokens · request 41 · 12 reads · 4 edits · 2 test runs · 15 min

The thinking itself is not printed, only how long the current one is (a long
one is how a step usually dies: it drafts code there and hits the output
limit). Sources: ``llm-live.txt`` (the llm proxy: every tool call as Qwen
issues it), ``<step>-attempt<n>/events.jsonl`` (opencode: the tool results)
and ``result.json`` (the watchdog's verdict). It only reads those files:
nothing can reach Qwen's session from here. Switches to a newer run by
itself. Ctrl-C stops.

    ./watch.py                       # the newest run of any plan
    ./watch.py runs/report/20261008-171421
    ./watch.py --actions             # one line per action, not only per phase
    ./watch.py --raw                 # the full stream: thinking, answers, tool calls
    ./watch.py --from-start          # replay the run from its beginning
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys
import time

RUNS = pathlib.Path(__file__).resolve().parent / "runs"
DIM, BOLD, CYAN, YELLOW, RED, GREEN, RESET = (
    "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[32m", "\033[0m")
CHARS_PER_TOKEN = 3.6  # Qwen's thinking, English prose: an estimate
LONG_THINKING = 6000   # tokens: shown in yellow (the reply limit is 16k)

# Commands shown as "testing" / "building"; add your project's own runners here.
TEST_RE = re.compile(r"pytest|unittest|cargo test|go test|npm (run )?test|node --test|make (test|check)|ctest")
BUILD_RE = re.compile(r"cargo (build|clippy|fmt|check)|go (build|vet)|npm run build|tsc\b|make\b|cmake --build")
WRITE_RE = re.compile(r"(^|[;&|]\s*)(cat|printf|echo)\b[^|;&]*>\s*\S|\btee\b|\bmv\b|\bcp\b|\brm\b")
READ_RE = re.compile(r"^\s*(sed -n|cat|grep|rg|ls|find|wc|head|tail|du|file|stat|diff)\b")
SUMMARY_RES = [
    re.compile(r"^[\w ()-]*: \d+ passed.*$", re.M),
    re.compile(r"^test result: \w+\. \d+ passed; \d+ failed.*$", re.M),
    re.compile(r"^=*\s*\d+ (passed|failed)[^=\n]*", re.M),
    re.compile(r"^ℹ (pass|fail) \d+$", re.M),
    re.compile(r"^error(\[E\d+\])?: .*$", re.M),
]


def newest_run() -> pathlib.Path | None:
    runs = [p for p in RUNS.glob("*/*") if p.is_dir() and p.name[:8].isdigit()]
    return max(runs, key=lambda p: p.stat().st_mtime, default=None)


class Follow:
    """New complete lines of a growing file."""

    def __init__(self, path: pathlib.Path, from_end: bool):
        self.path = path
        self.pos = path.stat().st_size if from_end and path.exists() else 0

    def lines(self) -> list[str]:
        if not self.path.exists():
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read()
        cut = data.rfind(b"\n") + 1  # keep a partial line for the next round
        self.pos += cut
        return data[:cut].decode("utf-8", "replace").splitlines()


def rel(path: str) -> str:
    return path.replace("/work/", "").replace("/work", ".")


def describe(tool: str, args: dict) -> tuple[str, str]:
    """(phase, short text) for one tool call."""
    path = rel(str(args.get("path") or args.get("filePath") or ""))
    if tool == "read":
        rng = ""
        if args.get("limit"):
            start = int(args.get("offset") or 1)
            rng = f" ({start}-{start + int(args['limit'])})"
        elif args.get("offset"):
            rng = f" (from {args['offset']})"
        return "reading", f"{path}{rng}"
    if tool in ("grep", "glob", "list"):
        return "reading", f"{tool} {args.get('pattern', '')} {path}".strip()
    if tool in ("write", "edit", "patch", "multiedit"):
        if not path:
            return "writing", f"{tool} (the call was cut off)"
        extra = f" ({len(str(args.get('content', '')).splitlines())} lines)" if tool == "write" else ""
        return "writing", f"{tool} {path}{extra}"
    if tool in ("todowrite", "todoread"):
        return "planning", "its todo list"
    if tool in ("shell", "bash"):
        original = str(args.get("command", ""))
        cmd = re.sub(r"cd /work(/\S+)? && ", "", original).replace("/work/", "")
        cmd = " ".join(cmd.split())[:110]
        if TEST_RE.search(original):
            return "testing", cmd
        if BUILD_RE.search(original):
            return "building", cmd
        if WRITE_RE.search(cmd):
            return "writing", cmd
        if READ_RE.search(cmd):
            return "reading", cmd
        return "running commands", cmd
    return "running commands", tool


def test_summary(output: str) -> str | None:
    found = []
    for rx in SUMMARY_RES:
        found += [m.group(0).strip() for m in rx.finditer(output)]
    return " | ".join(dict.fromkeys(found))[:160] or None


_status = ""
_clock = None  # HH:MM of the newest request in llm-live.txt (so replays show the real times)


def clock() -> str:
    return _clock or time.strftime("%H:%M")


def out(text: str):
    """A line above the status line."""
    sys.stdout.write("\r\033[K" + text + "\n" + _status)
    sys.stdout.flush()


def status(text: str):
    global _status
    width = shutil.get_terminal_size((120, 20)).columns
    visible = re.sub(r"\033\[\d+m", "", text)
    _status = text if len(visible) <= width else text[: width - 1]
    sys.stdout.write("\r\033[K" + _status)
    sys.stdout.flush()


class Attempt:
    """The phases of one attempt, printed as they change."""

    def __init__(self, folder: pathlib.Path, actions: bool, at: str | None = None, joined: bool = False):
        global _clock
        if at:
            _clock = at
        self.folder, self.actions = folder, actions
        self.phase, self.n = None, 0
        self.reads = self.edits = self.tests = 0
        self.tested = self.done_shown = self.result_shown = False
        prompt = folder / "prompt.md"  # written when the attempt starts
        self.started = prompt.stat().st_mtime if prompt.exists() else time.time()
        step, _, num = folder.name.partition("-attempt")
        out(f"\n{BOLD}■ {step}, attempt {num} ({clock()}){RESET}")
        if joined:  # already running when watch.py started: only what happens from now on
            _clock = time.strftime("%H:%M")
            out(f"{DIM}  joined at {_clock}, the earlier phases: ./watch.py --from-start{RESET}")
        else:
            self.enter("understanding the task")

    def enter(self, phase: str, text: str = ""):
        if phase == "writing" and self.tested:
            phase = "fixing"
        if phase != self.phase:
            self.phase, self.n = phase, self.n + 1
            colour = {"testing": CYAN, "fixing": YELLOW, "writing": GREEN}.get(phase, "")
            out(f"[{clock()}] {colour}{self.n} {phase}{RESET}" + (f"  {DIM}· {text}{RESET}" if text else ""))
        elif text and (self.actions or phase in ("testing", "writing", "fixing")):
            out(f"{' ' * 9}{DIM}· {text}{RESET}")

    def tool_call(self, tool: str, args: dict):
        phase, text = describe(tool, args)
        self.reads += phase == "reading"
        self.edits += phase == "writing"
        self.tests += phase == "testing"
        self.enter(phase, text)
        self.tested = self.tested or phase == "testing"

    def event(self, ev: dict, replay: bool = False):
        """A tool result (and, in a replay, the call itself: live calls come from llm-live.txt)."""
        global _clock
        part = ev.get("part") or {}
        if ev.get("type") == "tool_use":
            st = part.get("state") or {}
            tool = part.get("tool", "?")
            if replay:
                start = (st.get("time") or {}).get("start")
                if start:
                    _clock = time.strftime("%H:%M", time.localtime(start / 1000))
                self.tool_call(tool, st.get("input") if isinstance(st.get("input"), dict) else {})
            output = st.get("output") if isinstance(st.get("output"), str) else json.dumps(st.get("output"))
            if st.get("status") == "error":
                out(f"{' ' * 9}{RED}↳ {tool} failed: {str(st.get('error') or output)[:140]}{RESET}")
            elif tool in ("shell", "bash") and TEST_RE.search(str((st.get("input") or {}).get("command", ""))):
                summary = test_summary(output or "")
                if summary:
                    bad = re.search(r"[1-9]\d* failed|FAILED|fail [1-9]|error", summary)
                    out(f"{' ' * 9}{RED if bad else GREEN}↳ {summary}{RESET}")
        elif ev.get("type") == "text" and "SUMMARY:" in str(part.get("text", "")) and not self.done_shown:
            self.done_shown = True
            self.enter("done: the watchdog verifies")

    def check_result(self):
        res = self.folder / "result.json"
        if self.result_shown or not res.exists():
            return
        try:
            d = json.loads(res.read_text())
        except ValueError:
            return  # still being written
        self.result_shown = True
        mark = f"{GREEN}✔ committed" if d.get("ok") else f"{RED}✘ failed"
        out(f"[{clock()}] {mark}{RESET} after {d.get('minutes')} min: {d.get('reason')}")


class Live:
    """llm-live.txt: requests, thinking length, tool calls (emitted when complete)."""

    def __init__(self):
        self.request, self.kind, self.tool, self.buf, self.thinking = None, None, None, [], 0
        self.messages = 0  # of the previous request: a sharp drop means opencode compacted

    def feed(self, line: str, attempt: Attempt | None):
        if line.startswith("════ request"):
            global _clock
            self.flush(attempt)
            t = re.search(r"· (\d\d:\d\d):\d\d ·", line)
            _clock = t.group(1) if t else _clock
            m = re.match(r"════ request (\d+)", line)
            self.request, self.kind, self.thinking = (m.group(1) if m else "?"), None, 0
            n = re.search(r"· (\d+) messages", line)
            n = int(n.group(1)) if n else 0
            if attempt and self.messages >= 20 and n < self.messages / 4:
                out(f"{' ' * 9}{YELLOW}⟲ context full: opencode summarised {self.messages} messages, Qwen starts over from the summary{RESET}")
            self.messages = n
        elif line.startswith("── "):
            self.flush(attempt)
            self.kind = line[3:].split(" ──")[0]
            if self.kind.startswith("tool call: "):
                self.tool = self.kind[len("tool call: "):]
            elif self.kind.startswith("finished: length") and attempt:
                out(f"{' ' * 9}{RED}⚠ reply cut off at the output limit (thinking ~{self.tokens()} tokens){RESET}")
        elif self.kind == "thinking":
            self.thinking += len(line) + 1
            if attempt and "thinking budget is used up" in line:
                out(f"{' ' * 9}{DIM}⏱ thinking budget reached (~{self.tokens()} tokens): told to write now{RESET}")
        elif self.tool is not None:
            self.buf.append(line)

    def flush(self, attempt: Attempt | None):
        if self.tool is not None and attempt:
            text = "\n".join(self.buf).strip()
            try:
                args = json.loads(text) if text else {}
            except ValueError:
                args = {"command": text[:200]} if self.tool in ("shell", "bash") else {}
            attempt.tool_call(self.tool, args if isinstance(args, dict) else {})
        self.tool, self.buf = None, []

    def tokens(self) -> str:
        t = self.thinking / CHARS_PER_TOKEN
        return f"{t / 1000:.1f}k" if t >= 1000 else f"{int(t)}"


def first_time(folder: pathlib.Path) -> str | None:
    """HH:MM of an attempt's first event."""
    try:
        with open(folder / "events.jsonl") as f:
            ms = json.loads(f.readline()).get("timestamp")
        return time.strftime("%H:%M", time.localtime(ms / 1000)) if ms else None
    except (OSError, ValueError):
        return None


def raw_colour(text: str) -> str:
    lines, kind = [], None
    for line in text.splitlines(keepends=True):
        if line.startswith("════"):
            lines.append(f"{BOLD}{line}{RESET}")
        elif line.startswith("── "):
            kind = line[3:].split(" ──")[0]
            lines.append(f"{YELLOW if kind.startswith('tool call') else CYAN}{line}{RESET}")
        else:
            lines.append(f"{DIM}{line}{RESET}" if kind == "thinking" else line)
    return "".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="?", help="a run folder (default: the newest)")
    ap.add_argument("--actions", action="store_true", help="one line per action, not only per phase")
    ap.add_argument("--raw", action="store_true", help="the full stream, thinking included")
    ap.add_argument("--from-start", action="store_true", help="replay the run from its beginning")
    args = ap.parse_args()
    fixed = pathlib.Path(args.run).resolve() if args.run else None
    run = live = events = attempt = None
    folder = None
    parser = Live()
    startup = True  # an attempt found in the first round was already running
    try:
        while True:
            current = fixed or newest_run()
            if current and current != run:
                run, attempt, events, folder, parser = current, None, None, None, Live()
                live = Follow(run / "llm-live.txt", from_end=not args.from_start)
                out(f"{BOLD}▶ watching {run.relative_to(RUNS) if run.is_relative_to(RUNS) else run}{RESET}")
            if run:
                folders = sorted((p.parent for p in run.glob("*-attempt*/events.jsonl")),
                                 key=lambda p: p.stat().st_mtime)
                if folders and folders[-1] != folder:
                    if attempt:
                        attempt.check_result()
                    if args.from_start and folder is None and not args.raw:
                        # a replay: each earlier attempt from its own events, then follow the newest one
                        for old in folders:
                            attempt = Attempt(old, args.actions, at=first_time(old))
                            for line in Follow(old / "events.jsonl", from_end=False).lines():
                                try:
                                    attempt.event(json.loads(line), replay=True)
                                except ValueError:
                                    continue
                            attempt.check_result()
                        folder = folders[-1]
                        events = Follow(folder / "events.jsonl", from_end=True)
                        live = Follow(run / "llm-live.txt", from_end=True)
                        continue
                    folder = folders[-1]
                    if args.raw:
                        out(f"{BOLD}■ {folder.name}{RESET}")
                    else:
                        attempt = Attempt(folder, args.actions, joined=startup,
                                          at=first_time(folder) if startup else time.strftime("%H:%M"))
                    events = Follow(folder / "events.jsonl", from_end=startup)
                lines = live.lines()
                if args.raw:
                    if lines:
                        sys.stdout.write(raw_colour("\n".join(lines) + "\n"))
                else:
                    for line in lines:
                        parser.feed(line, attempt)
                    if events and attempt:
                        for line in events.lines():
                            try:
                                attempt.event(json.loads(line))
                            except ValueError:
                                continue
                    if attempt:
                        attempt.check_result()
                        mins = (time.time() - attempt.started) / 60
                        long = parser.thinking / CHARS_PER_TOKEN > LONG_THINKING
                        if attempt.result_shown:
                            state = "attempt over: the watchdog checks, commits or starts the next one"
                        elif parser.kind == "thinking":
                            state = f"thinking {parser.tokens()} tokens" + (" (long: drafting?)" if long else "")
                        elif attempt.phase in ("testing", "building") and parser.tool is None:
                            state = f"waiting for the {attempt.phase[:-3]}"
                        else:
                            state = "working"
                        status(f"{YELLOW if long else DIM}  … {state} · request {parser.request or '-'} · "
                               f"{attempt.reads} reads · {attempt.edits} edits · {attempt.tests} test runs · "
                               f"{mins:.0f} min{RESET}")
            startup = False
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
