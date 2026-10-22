#!/usr/bin/env python3
"""local-llm-harness: runs a plan of sub-steps through opencode without a human.

Per step: start a fresh sandbox container (see sandbox.py: Qwen gets a
shell there, but no network except llama-server, no git, no podman) and a
fresh opencode session in it; watch for stalls (opencode events plus the
llm proxy's per-request stats, see llmproxy.py); then check the result
itself in a fresh verification container (allowed paths, no symlinks or
nested .git, the verify commands, test count, line limit) and commit.
Failed attempts are saved, reset and retried with the failure reason;
after max_attempts the watchdog writes report.md, notifies and stops.

The watchdog, its config, plans and logs live outside the worktree, so
Qwen can't change them. Every git call on the host runs with hooks and
fsmonitor disabled and checks that the worktree's `.git` pointer file is
unchanged, so nothing Qwen writes into the worktree runs on the host.

The plan format is described in README.md ("Plan reference").

Usage: watchdog.py PLAN.json [--dry-run] [--once]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import fnmatch
import hashlib
import json
import os
import pathlib
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

import llmproxy
import sandbox

HERE = pathlib.Path(__file__).resolve().parent
KEY_FILE = pathlib.Path(os.environ.get("WATCHDOG_KEY_FILE", pathlib.Path.home() / ".config/llm/api-key"))
CONTEXT_TOKENS = 131072  # default for llama-server's -c (the whole window: prompt + reply); plan: context_tokens
OPENCODE_CONTEXT = 100000  # default for what opencode is told (sandbox opencode.json); plan: opencode_context
GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
WATCHDOG_DIR = ".watchdog"  # in the worktree, git-excluded (see exclude_watchdog_dir)
REVIEW_INPUT = f"{WATCHDOG_DIR}/review-input/diff.patch"
REVIEW_REPORT = f"{WATCHDOG_DIR}/review-report.md"

PREAMBLE = """\
# How this run works (read first)

You are running unattended in a sandbox, driven by a watchdog script.

- You have a shell (bash) inside a sandbox container. The repo is at `/work`. There is no network and no git.{environment}
- Only change files under the allowed paths for this step: {allowed}. Changing anything else, or creating symlinks, makes the attempt fail.
- Before you finish, run these checks yourself and fix what fails:
{verify}
  The watchdog runs exactly these commands again in a fresh container, then commits. Don't try to commit.
- Keep your thinking to a short plan (at most 15 lines), then act. No code blocks in your thinking: write the code into the file and edit it there. If you are unsure how a library behaves, write the code and run it instead of deliberating. Write long files in parts.
- What the task lists as decided was verified beforehand: use it as given, don't re-derive it.
{paths}
- When you are done, end your final reply with exactly one line: `SUMMARY: <commit summary, at most 60 characters>`.

"""

# Both preambles: the read tool was given `/app/main.py` (not `/work/...`), and opencode
# resolves `cd ..` against /work, so `cd sub && ... && cd .. && ...` was blocked.
PATHS_RULE = ("- Paths: give the read/edit/write tools absolute paths under `/work` (`/work/src/main.py`, "
              "not `/src/main.py`). In the shell, `cd` only to absolute paths (`cd /work/sub`, `cd /work`): "
              "`cd ..` is blocked as a path outside the repo.")

REVIEW_PREAMBLE = """\
# How this run works (read first)

You are running unattended in a sandbox, driven by a watchdog script. This is a **read-only review**.

- You have a shell (bash) inside a sandbox container. The repo is at `/work`. There is no network and no git. The diff under review is in `/work/{diff}`, and the prompt file the steps were built from (their spec) is next to it as `spec.md`.
- Don't change any file. The only file you write is the report, `/work/{report}`.
- Write the report as you go: create it after the checks (heading + the checks section), then add each section with an `edit` as soon as you have checked that item, and the verdict last. Never draft the report in your thinking, and never write it in one big call.
- Keep the context small: `rg` and `sed -n` line ranges instead of whole files, `| tail` on long command output. Run each check once.
- You may run the tests to check a finding: {verify}
- Keep your thinking to a short plan, then work through the checklist.
{paths}
- When you are done, end your final reply with exactly one line: `SUMMARY: <one-line verdict, at most 60 characters>`.

"""

RETRY = """
# The previous attempt failed

{reason}

{details}

The worktree was reset to the last good commit. Start again from there and avoid this failure.
"""

LENGTH_HINT = """Your reply was cut off at the output limit after {edits} file edit(s) in this attempt.
Almost always that means the code was drafted in the thinking instead of written to files.
This time: plan in at most a few lines, then write the first file with the write tool in
that same reply. One file per reply, build or run it, then the next file.
No code blocks in your thinking: the files are your draft, and you can edit them afterwards."""

CONTEXT_HINT = """The session grew too long for the model ({tokens} tokens; the window holds {ctx}), so its reply was cut off.
That is not long thinking: the session had read too much ({reads} file reads, {edits} edit(s)).
This time read only the files the task names, and only the parts you need: `grep -n` for a
name, then `sed -n 'A,Bp'` or a read with offset/limit for those lines. Don't read a file twice.
Write the first file within your first ten tool calls, then compile; the files keep your work,
the context doesn't."""

TOOLCALL_HINT = """Your last tool call was cut off in the middle of its JSON, so llama-server could not
parse it. That happens when one call carries a very large text (usually a `write` of a whole
big file). Write big files in parts: `write` the first part (up to ~150 lines), then add the
rest with `edit` calls that anchor on the last lines already in the file."""

TOOL_ERRORS_HINT = """These were the last errors (tool, target, error):
{errors}

What to do:
{advice}- Read the error text before you retry a call; an identical call fails the same way."""

# Observed: opencode summarised one session 15 times in 75 min; after each summary Qwen read
# the same files again (one test-harness file 38 times), made 6 edits in all and ran out of
# time. The watchdog now ends such an attempt early (plan: max_summaries_without_edit).
SUMMARY_LOOP_HINT = """The session filled up and was summarised {summaries} times without a single edit in
between: you read in circles. Most read: {most_read}.
This time: read each file the task names once, only the lines it names (`grep -n` for a
name, then a read with offset/limit). Write the first file within ten tool calls; after
that, take what you need from the checks' failure lines, not from the test harness's files."""

TIMEOUT_HINT = """The attempt ran out of time. {progress}
Work in smaller pieces this time: write one file, build or run it, fix it, then
the next one. Run the full checks once at the end, not after every small change."""

STALL_HINT = """Nothing happened for a long time ({in_flight}). {progress}
Usually one reply grew very long (long thinking). Keep the thinking to a short plan and
put the code into files with the write/edit tools."""

# Review steps write one report, not code (observed: the whole report drafted in the thinking,
# then written in one call, cut off at the output limit after 109 requests).
REVIEW_LENGTH_HINT = """Your reply was cut off at the output limit after {edits} edit(s) of the report.
That happens when the report is drafted in the thinking and then written in one call.
This time: run the checks, then create the report right away with the heading and the
checks section. Add each further section with an `edit` as soon as you have checked that
item, the verdict last. No report text in your thinking: the file is your draft."""

REVIEW_TOOLCALL_HINT = """Your last tool call was cut off in the middle of its JSON, so llama-server could not
parse it. That happens when one call carries the whole report. Write the report in parts:
create it with the heading and the first section, then add one section per `edit` call,
anchored on the last lines already in the file."""

REVIEW_SLOW_HINT = """{what} {progress}
The context grew too big or one reply ran too long. This time: create the report right after
the checks and add each section as soon as you have checked it, so the work is saved as you
go. Use `rg` and `sed -n` line ranges instead of whole files, and `| tail` on long output."""

# Observed: Qwen read its own PNG screenshots; llama-server had no mmproj, so every request
# after that was a 500 and opencode gave up. llmproxy now strips images too.
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
IMAGE_HINT = """You opened {files} with the read tool. This model reads text only: the
image made every following request fail ("image input is not supported"), and the session ended.
This time: don't create or open screenshots or any other image file (and don't copy images into
the repo to read them). The checks' text output is your view of the result; people review images."""

ERROR_HINT = """opencode stopped on this error:
{error}

{progress}"""


def retry_hint(reason, st, review=False, context_tokens=CONTEXT_TOKENS):
    """Concrete advice for a failure that happened while Qwen worked (verification
    failures bring their own details). Empty when there is nothing useful to say."""
    files = st.get("edited_files") or []
    progress = (f"It had written {', '.join(files)} (discarded: the worktree was reset)."
                if files else "No file had been written yet.")
    if review:
        progress = ("The report had been started." if files
                    else "The report had not been started yet: all findings were lost.")
    if reason.startswith("the model's context was full"):
        llm = st.get("llm", {})
        return CONTEXT_HINT.format(tokens=llm.get("length_stop_context", "?"), ctx=context_tokens,
                                   reads=st.get("reads", "?"), edits=st.get("edits", 0))
    if reason.startswith("hit the output-token limit"):
        return (REVIEW_LENGTH_HINT if review else LENGTH_HINT).format(edits=st.get("edits", 0))
    if reason.startswith("llama-server could not parse a tool call"):
        return REVIEW_TOOLCALL_HINT if review else TOOLCALL_HINT
    if reason.endswith("failed tool calls in a row"):
        errors = st.get("last_tool_errors") or []
        text = " ".join(errors)
        advice = ""
        if "Could not find oldString" in text:
            advice += ("- `oldString` didn't match: read the current lines first (`sed -n` or read), "
                       "because formatters and your earlier edits change them.\n")
        if "Permission denied: read" in text:
            advice += ("- Those files are blocked for reading (the test harness's own code). What you need is in "
                       "the task and in the checks' FAIL lines; don't try to read them another way.\n")
        if "external_directory" in text:
            advice += ("- Paths outside the repo are blocked: the repo is `/work` (e.g. `/work/src/...`). "
                       "In the shell, `cd` only to absolute paths (`cd /work/sub`); `cd ..` counts as outside.\n")
        return TOOL_ERRORS_HINT.format(errors="\n".join(f"- {e}" for e in errors) or "- (none recorded)",
                                       advice=advice)
    if reason.startswith("the session was summarised"):
        most = st.get("most_read") or {}
        return SUMMARY_LOOP_HINT.format(
            summaries=st.get("summaries", "?"),
            most_read=", ".join(f"{f} ({n}x)" for f, n in most.items()) or "(none recorded)")
    if reason.startswith("wall-clock timeout"):
        if review:
            return REVIEW_SLOW_HINT.format(what="The attempt ran out of time.", progress=progress)
        return TIMEOUT_HINT.format(progress=progress)
    if reason.startswith("no progress for"):
        in_flight = reason[reason.find(";") + 1:reason.rfind(")")].strip() or "no llm request in flight"
        if review:
            return REVIEW_SLOW_HINT.format(what=f"Nothing happened for a long time ({in_flight}).",
                                           progress=progress)
        return STALL_HINT.format(in_flight=in_flight, progress=progress)
    last_error = st.get("last_error") or ""
    if "image input is not supported" in last_error:
        return IMAGE_HINT.format(files=", ".join(st.get("images_opened") or []) or "an image file")
    if last_error:
        return ERROR_HINT.format(error=last_error, progress=progress)
    return ""


# --- helpers -------------------------------------------------------------

def sh(args, cwd=None, check=True, timeout=None, env=None):
    return subprocess.run(args, cwd=cwd, check=check, timeout=timeout, env=env,
                          text=True, capture_output=True)


class Git:
    """git on the host, hardened against anything Qwen wrote into the worktree."""

    def __init__(self, wt: pathlib.Path):
        self.wt = wt
        self.pointer = (wt / ".git").read_text() if (wt / ".git").is_file() else None

    def __call__(self, *args, check=True):
        dotgit = self.wt / ".git"
        if dotgit.is_symlink() or not dotgit.is_file() or dotgit.read_text() != self.pointer:
            raise RuntimeError(f"{dotgit} was changed; refusing to run git there")
        return sh(["git", *GIT_SAFE, "-C", str(self.wt), *args], check=check).stdout


def notify(title, body):
    print(f"\n*** {title}: {body}\n", flush=True)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "local-llm-harness", title, body], check=False)


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def matches(path, patterns):
    """fnmatch with `*` also crossing `/` (so `tests/*` covers subfolders)."""
    return any(fnmatch.fnmatch(path, p) for p in patterns)


# --- plan and worktree ---------------------------------------------------

# Files a step may never change, whatever the plan allows: git internals and
# opencode's own config (Qwen could change its permissions), and the watchdog's
# folder in the worktree. A plan adds its own (e.g. files that run on the host).
ALWAYS_FORBIDDEN = [".git", ".git/*", "*/.git", "*/.git/*", ".opencode/*", "opencode.json",
                    f"{WATCHDOG_DIR}/*"]

def load_plan(path):
    plan = json.loads(pathlib.Path(path).read_text())
    if "sandbox" not in plan:
        sys.exit("this watchdog version needs a 'sandbox' block in the plan")
    if plan.get("base") and plan.get("base") == plan.get("branch"):
        # done steps are the commits in base..branch: a base that moves with the branch
        # never shows one, so a committed step would be started again
        sys.exit(f"plan: 'base' is the branch itself ({plan['base']}); use the commit to start from (its sha)")
    plan.setdefault("max_attempts", 3)
    plan.setdefault("attempt_timeout_min", 45)
    plan.setdefault("idle_timeout_min", 12)
    plan.setdefault("max_tool_errors_in_row", 6)
    plan.setdefault("verify_timeout_s", 1800)
    plan.setdefault("verify", [])
    plan.setdefault("protected", [])
    plan.setdefault("context_tokens", CONTEXT_TOKENS)
    plan.setdefault("opencode_context", OPENCODE_CONTEXT)
    plan.setdefault("fail_patterns", [r"^FAIL (\S+)"])
    plan.setdefault("passed_pattern", r"(\d+) passed")
    if "image" not in plan["sandbox"]:
        sys.exit("the plan's sandbox block needs an 'image'")
    plan["forbidden"] = plan.get("forbidden", []) + ALWAYS_FORBIDDEN
    plan["name"] = pathlib.Path(path).stem
    return plan


def ensure_worktree(plan):
    wt = pathlib.Path(plan["worktree"])
    if wt.exists():
        return wt
    repo = ["git", *GIT_SAFE, "-C", plan["repo"]]
    if sh([*repo, "branch", "--list", plan["branch"]]).stdout.strip():
        sh([*repo, "worktree", "add", str(wt), plan["branch"]])
    else:
        sh([*repo, "worktree", "add", "-b", plan["branch"], str(wt), plan["base"]])
    log(f"created worktree {wt} on branch {plan['branch']}")
    return wt


def reset_worktree(git, commit):
    git("reset", "-q", "--hard", commit)
    git("clean", "-q", "-fdx")


def done_steps(plan, git):
    subjects = git("log", f"{plan['base']}..HEAD", "--format=%s").splitlines()
    return {s.split(":", 1)[0].removeprefix("Step ").strip() for s in subjects if s.startswith("Step ")}


def changed_paths(git):
    out = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths, parts = [], out.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        if not entry:
            i += 1
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status or "C" in status:  # rename/copy: next field is the source
            i += 1
            paths.append(parts[i])
        i += 1
    return paths


def unsafe_paths(wt, paths):
    """Symlinks and nested repositories among the changes."""
    bad = []
    for p in paths:
        full = wt / p
        if full.is_symlink() or p.rstrip("/").endswith(".git") or "/.git/" in f"/{p}":
            bad.append(p)
        elif full.is_dir():  # a nested repo shows up as a directory entry
            bad.append(p)
    return bad


def too_long(wt, plan, paths):
    """The changed code files over the plan's hard limit (they fail the step).
    ``max_lines``: {"patterns": [...], "warn": N, "limit": M}, optional."""
    rule = plan.get("max_lines")
    out = []
    if not rule:
        return out
    for p in paths:
        full = wt / p
        if full.is_file() and matches(p, rule["patterns"]):
            n = sum(1 for _ in full.open(errors="replace"))
            if n > rule["limit"]:
                out.append(f"{p}: {n} lines (limit {rule['limit']})")
    return out


def long_files_note(git, wt, plan, step):
    """The prompt's note on the long code files the step may change (over the
    warn line): a signal to put new code into a new file, not a failure."""
    rule = plan.get("max_lines")
    long = []
    if not rule:
        return ""
    for p in git("ls-files").splitlines():
        full = wt / p
        if full.is_file() and matches(p, rule["patterns"]) and matches(p, step.get("allowed", [])):
            n = sum(1 for _ in full.open(errors="replace"))
            if n > rule["warn"]:
                long.append(f"`{p}` ({n} lines)")
    if not long:
        return ""
    return (f"\n\n## Long files\n\nThese files you may change have more than {rule['warn']} lines: "
            f"{', '.join(long)}. Put new code into a new file rather than growing them; "
            f"a code file over {rule['limit']} lines fails the step.\n")


def count_passed(plan, output, step=None):
    """The passed-test count of a verify output, from the plan's own ``verify``
    commands only (``passed_pattern``, summed). A step's ``extra_verify`` may
    print "N passed" too; counting it would raise the next step's baseline
    (observed: a correct step failed "fewer passing tests" because of that)."""
    base = {c.split("\n")[0] for c in verify_commands(plan, step)[:len(plan["verify"])]}
    total, on = 0, False
    for line in output.splitlines():
        if line.startswith("$ "):
            on = line[2:] in base
        elif on:
            total += sum(int(n) for n in re.findall(plan["passed_pattern"], line))
    return total


# --- failing checks (preflight, retry notes) ------------------------------

def failing_checks(plan, output):
    """The failing checks named in a verify output: every match of the plan's
    ``fail_patterns`` (a regex whose group 1 is the check's name, or
    {"regex": ..., "prefix": "fault/"} to tell kinds apart)."""
    names = set()
    for pat in plan["fail_patterns"]:
        rx, prefix = (pat, "") if isinstance(pat, str) else (pat["regex"], pat.get("prefix", ""))
        names |= {prefix + n for n in re.findall(rx, output, re.M)}
    return names


def failed_commands(output):
    """The verify commands that failed (sandbox.verify runs all of them)."""
    failed, cmd = [], None
    for line in output.splitlines():
        if line.startswith("$ "):
            cmd = line[2:]
        elif line.startswith("[this command failed") and cmd:
            failed.append(cmd)
    return failed


def required_by(step, name):
    """Whether the step's ``require`` patterns require this check (an include
    matches, no ! exclude does)."""
    pats = step.get("require", [])
    return (any(fnmatch.fnmatch(name, p) for p in pats if not p.startswith("!"))
            and not any(fnmatch.fnmatch(name, p[1:]) for p in pats if p.startswith("!")))


def preflight_problem(step, base_failing):
    """None, or why the step can't start: checks it requires fail on the clean
    base, and it isn't meant to fix them (its ``fixes`` patterns). Qwen can't
    repair those (observed: a stale base cost three attempts and 3.5 h)."""
    if base_failing is None:
        return None
    stuck = sorted(n for n in base_failing if required_by(step, n) and not matches(n, step.get("fixes", [])))
    if not stuck:
        return None
    return (f"{len(stuck)} checks that step {step['id']} requires already fail on the clean base, and the step "
            f"isn't meant to fix them (its `fixes`): {', '.join(stuck[:12])}{' …' if len(stuck) > 12 else ''}. "
            "Fix the base (e.g. regenerate stale expected outputs), or exclude them (`!pattern`) or add them to the step's `fixes`.")


def failure_note(plan, step, output, base_failing):
    """What the retry prompt says about a failed verification: which commands
    failed, which required checks fail because of the change, which are the
    step's job; failures that are neither are not required."""
    now_failing = failing_checks(plan, output)
    required = sorted(n for n in now_failing if required_by(step, n))
    lines = []
    cmds = failed_commands(output)
    if cmds:
        lines.append("Failed commands: " + "; ".join(f"`{c[:100]}`" for c in cmds))
    if base_failing is None:
        if required:
            lines.append("Required checks failing: " + ", ".join(required[:15]))
    else:
        mine = [n for n in required if n not in base_failing]
        todo = [n for n in required if n in base_failing]
        if mine:
            lines.append("**Failing because of your change** (they passed before it): " + ", ".join(mine[:15]))
        if todo:
            lines.append("Still failing, and this step must make them pass: " + ", ".join(todo[:15]))
    other = len(now_failing) - len(required)
    if other:
        lines.append(f"{other} other failing checks are not required in this step: ignore them.")
    return "\n".join(f"- {l}" for l in lines)


# --- verified trees (skip a baseline that would repeat a passed check) ----

def verify_fingerprint(plan):
    """What a verification result depends on besides the tree: the
    baseline commands and the sandbox image."""
    image = plan["sandbox"]["image"]
    image_id = sh(["podman", "image", "inspect", "--format", "{{.Id}}", image], check=False).stdout.strip()
    return hashlib.sha256(json.dumps([verify_commands(plan), image_id]).encode()).hexdigest()


def verified_cache(plan):
    return HERE / "runs" / plan["name"] / "verified.json"


def cached_entry(plan, git):
    """The cache entry of HEAD's tree (passed count, failing checks) if that
    exact tree already passed with the same commands and image, else None."""
    try:
        entries = json.loads(verified_cache(plan).read_text())
    except (OSError, ValueError):
        return None
    entry = entries.get(git("rev-parse", "HEAD^{tree}").strip())
    if entry and entry.get("fingerprint") == verify_fingerprint(plan):
        return entry
    return None


def remember_verified(plan, git, passed, output="", failing=None):
    """Record that HEAD's tree passed the checks. A step's own check ran
    with its requirements, a superset of the baseline's, so a pass there is
    a pass for the baseline too. Not recorded while ignored files lie in the
    worktree (outside the watchdog's folder and Python's __pycache__/, which
    can't change a result): the check saw them, but the next baseline runs
    after `git clean` and would not."""
    stray = [p for p in git("ls-files", "--others", "--ignored", "--exclude-standard", "--directory").splitlines()
             if p and not p.startswith(f"{WATCHDOG_DIR}/") and not p.rstrip("/").endswith("__pycache__")]
    if stray:
        log(f"not caching the verification: ignored files in the worktree ({', '.join(stray[:5])})")
        return
    path = verified_cache(plan)
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError):
        entries = {}
    if failing is None and output:
        failing = failing_checks(plan, output)
    entries[git("rev-parse", "HEAD^{tree}").strip()] = {
        "fingerprint": verify_fingerprint(plan), "passed": passed,
        "failing": sorted(failing) if failing is not None else None, "time": now()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(list(entries.items())[-50:]), indent=1) + "\n")


# --- one attempt ---------------------------------------------------------

def run_attempt(plan, step, wt, prompt, adir, sock_dir, stats):
    """Run one opencode session in a fresh sandbox. Returns (ok, reason, stats)."""
    name = f"wd-{plan['name']}-{step['id']}".lower()
    oc_data = adir / "opencode-data"
    oc_data.mkdir()
    sandbox.start(plan, wt, name, sock_dir, oc_data)
    cmd = sandbox.opencode_cmd(name, prompt, f"watchdog {plan['name']} {step['id']}", plan.get("model"))
    events = open(adir / "events.jsonl", "w")
    stderr = open(adir / "stderr.txt", "w")
    proc = subprocess.Popen(cmd, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr)
    lines: queue.Queue = queue.Queue()

    def pump():
        for l in proc.stdout:
            lines.put(l)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    st = {"steps": 0, "tool_calls": 0, "tool_errors": 0, "shell_calls": 0, "edits": 0, "reads": 0,
          "session": None, "summaries": 0}
    read_counts: collections.Counter = collections.Counter()
    prev_messages, loop_summaries, edits_at_summary = 0, 0, -1
    max_loop = plan.get("max_summaries_without_edit", 4)
    first_ev = len(stats.events)
    seen = first_ev
    start = last = time.monotonic()
    errors_in_row, final_text, reason, last_error = 0, "", None, ""
    edited, recent_errors, images = [], [], []
    wall_min = step.get("attempt_timeout_min", plan["attempt_timeout_min"])
    wall, idle = wall_min * 60, plan["idle_timeout_min"] * 60

    while True:
        try:
            line = lines.get(timeout=5)
        except queue.Empty:
            line = ""
        t = time.monotonic()
        for ev in stats.since(seen):  # llm proxy: per-request results
            seen += 1
            messages = ev.get("messages") or 0
            if prev_messages >= 20 and messages < prev_messages / 4:  # opencode summarised the session
                st["summaries"] += 1
                loop_summaries = loop_summaries + 1 if st["edits"] == edits_at_summary else 1
                edits_at_summary = st["edits"]
                if loop_summaries >= max_loop:
                    reason = f"the session was summarised {loop_summaries} times without an edit (reading in circles)"
            prev_messages = messages or prev_messages
            if ev.get("finish_reason") == "length":
                used = (ev.get("prompt_tokens") or 0) + (ev.get("completion_tokens") or 0)
                if used >= 0.97 * plan["context_tokens"]:
                    reason = (f"the model's context was full ({ev.get('prompt_tokens')} prompt + "
                              f"{ev.get('completion_tokens')} output tokens of {plan['context_tokens']})")
                elif (ev.get("prompt_tokens") or 0) >= 0.75 * plan["opencode_context"]:
                    # at this size opencode summarises the session, and the summary ran out
                    reason = (f"the model's context was full: the session reached {ev.get('prompt_tokens')} "
                              "prompt tokens and its summary was cut off")
                else:
                    reason = (f"hit the output-token limit ({ev.get('completion_tokens')} tokens: "
                              "long thinking or a huge tool call)")
            elif ev.get("status") not in (200, None) and "parse tool call" in ev.get("error", ""):
                reason = "llama-server could not parse a tool call (truncated tool-call JSON)"
        if reason:
            break
        if t - start > wall:
            reason = f"wall-clock timeout ({wall_min} min)"
        elif t - max(last, stats.last_activity) > idle:
            reason = (f"no progress for {plan['idle_timeout_min']} min (stall; "
                      f"{stats.in_flight} llm request(s) in flight)")
        if reason or line is None:
            break
        if not line:
            continue
        last = time.monotonic()
        events.write(line)
        events.flush()
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        st["session"] = st["session"] or ev.get("sessionID")
        part = ev.get("part", {})
        if ev.get("type") == "step_finish":
            st["steps"] += 1
            if part.get("reason") == "length":
                reason = "hit the output-token limit (long thinking or a huge tool call)"
                break
        elif ev.get("type") == "tool_use":
            st["tool_calls"] += 1
            tool = part.get("tool")
            st["shell_calls"] += tool in ("shell", "bash")
            st["edits"] += tool in ("edit", "write", "patch", "multiedit")
            st["reads"] += tool == "read"
            state = part.get("state", {})
            inp = state.get("input") if isinstance(state.get("input"), dict) else {}
            target = str(inp.get("path") or inp.get("filePath") or "")
            if tool == "read" and target:
                read_counts[target.removeprefix("/work/")] += 1
            if tool == "read" and target.lower().endswith(IMAGE_EXTS) and target not in images:
                images.append(target)
            if state.get("status") == "error":
                st["tool_errors"] += 1
                errors_in_row += 1
                target = inp.get("path") or inp.get("command") or inp.get("pattern") or ""
                recent_errors = (recent_errors + [
                    f"{tool} {str(target)[:120]}: {str(state.get('error', ''))[:300]}"])[-3:]
            else:
                errors_in_row = 0
                recent_errors = []
                path = str(inp.get("path") or "").removeprefix("/work/")
                if tool in ("edit", "write", "patch", "multiedit") and path and path not in edited:
                    edited.append(path)
            if errors_in_row > plan["max_tool_errors_in_row"]:
                reason = f"{errors_in_row} failed tool calls in a row"
                break
        elif ev.get("type") == "text":
            final_text = part.get("text", "") or final_text
        elif ev.get("type") == "error":  # opencode gave up on a provider error
            err = ev.get("error")
            last_error = str(err.get("message") or err if isinstance(err, dict) else err)[:500]

    if proc.poll() is None:
        sandbox.stop(name)  # kills the exec'd opencode with it
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    proc.wait()
    sandbox.stop(name)
    events.close()
    stderr.close()
    st["minutes"] = round((time.monotonic() - start) / 60, 1)
    st["llm"] = stats.summary(since=first_ev)
    st["final_text"] = final_text[-2000:]
    st["edited_files"] = edited
    st["last_tool_errors"] = recent_errors
    st["last_error"] = last_error
    st["images_opened"] = images
    st["most_read"] = dict(read_counts.most_common(4))
    if reason is None and proc.returncode != 0:
        reason = f"opencode exited with code {proc.returncode} (see stderr.txt)"
        if last_error:
            reason += f": {last_error[:150]}"
    return reason is None, reason, st


# --- verification --------------------------------------------------------

def verify_code(plan, step, git, wt, adir, baseline):
    """Check a code step. Returns (ok, reason, details, changed)."""
    paths = changed_paths(git)
    if not paths:
        return False, "no file was changed", "", paths
    bad = unsafe_paths(wt, paths)
    if bad:
        return False, "symlinks or nested repositories are not allowed", "\n".join(bad), paths
    bad = [p for p in paths if matches(p, plan["forbidden"]) or not matches(p, step["allowed"])]
    if bad:
        return False, "changed files outside the allowed paths", "\n".join(bad), paths
    long_files = too_long(wt, plan, paths)
    if long_files:
        return (False, f"code files over {plan['max_lines']['limit']} lines (split them)",
                "\n".join(long_files), paths)
    rc, output = sandbox.verify(plan, wt, verify_commands(plan, step), plan["verify_timeout_s"])
    (adir / "verify.txt").write_text(output)
    if rc != 0:
        return False, "verification failed", output[-6000:], paths
    passed = count_passed(plan, output, step)
    if passed < baseline and not step.get("allow_fewer_tests"):
        return False, f"fewer passing tests than before ({passed} < {baseline})", output[-2000:], paths
    return True, f"{passed} tests passed (before: {baseline})", "", paths


def verify_entries(plan, step=None):
    """The plan's ``verify`` entries plus the step's ``extra_verify``, each as
    (command, note): an entry is a command string or {"cmd": ..., "note": ...}
    (the note tells Qwen how to use the command, e.g. "slow: run only the
    part your change touches")."""
    out = []
    for e in plan["verify"] + (step or {}).get("extra_verify", []):
        out.append((e, "") if isinstance(e, str) else (e["cmd"], e.get("note", "")))
    return out


def fill(command, plan, step=None):
    """``{require}`` becomes the step's ``require`` patterns (nothing for the
    baseline); ``{name}`` becomes ``vars[name]`` (the step's vars over the
    plan's; a step var may be empty for the baseline)."""
    values = {**plan.get("vars", {}), **(step or {}).get("vars", {})}
    values["require"] = " ".join(shlex.quote(p) for p in (step or {}).get("require", []))
    for k, v in values.items():
        command = command.replace("{" + k + "}", str(v))
    return command.rstrip()


def qwen_verify_list(plan, step):
    """The verify commands as the prompt shows them, each with its note."""
    lines = [f"  - `{fill(c, plan, step)}`" + (f": {n}" if n else "") for c, n in verify_entries(plan, step)]
    return "\n".join(lines) or "  - (none)"


def verify_commands(plan, step=None):
    """The commands to run: the plan's ``verify`` (the baseline when step is
    None) plus the step's ``extra_verify`` (its own gates), placeholders filled."""
    return [fill(c, plan, step) for c, _ in verify_entries(plan, step)]


def verify_review(git, wt):
    paths = changed_paths(git)
    if paths:
        return False, "a review must not change files", "\n".join(paths)
    report = wt / REVIEW_REPORT
    if report.is_symlink() or not report.is_file() or report.stat().st_size < 50:
        return False, f"no report at {REVIEW_REPORT}", ""
    return True, "report written", ""


def exclude_watchdog_dir(git):
    """Make the worktree's .watchdog/ folder git-ignored (info/exclude of the
    repository), so review inputs and reports never show up as changes."""
    exclude = pathlib.Path(git("rev-parse", "--git-common-dir").strip())
    if not exclude.is_absolute():
        exclude = git.wt / exclude
    exclude = exclude / "info" / "exclude"
    text = exclude.read_text() if exclude.exists() else ""
    if f"/{WATCHDOG_DIR}/" not in text.split():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + f"/{WATCHDOG_DIR}/\n")


def prepare_review(plan, step, git, wt):
    """Write the phase diff for a review step into the (git-excluded) .watchdog/ folder."""
    exclude_watchdog_dir(git)
    since = step["review_since"]
    line = git("log", f"{plan['base']}..HEAD", "--format=%H %s", "--reverse")
    first = next((l.split()[0] for l in line.splitlines() if l.split(" ", 1)[1].startswith(f"Step {since}:")), None)
    if first is None:
        raise RuntimeError(f"review step {step['id']}: no commit 'Step {since}:' found")
    diff = git("diff", f"{first}~1..HEAD", "--", *step.get("review_paths", ["."]))
    target = wt / REVIEW_INPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(diff)
    # the phase's prompt file (the spec the steps were built from)
    spec = pathlib.Path(plan["prompts_dir"]) / step["prompt"].partition("#")[0]
    (target.parent / "spec.md").write_text(spec.read_text())


def save_diff(git, adir):
    git("add", "-A")
    (adir / "diff.patch").write_text(git("diff", "--cached", "--binary"))
    git("reset", "-q")


def summary_line(step, final_text):
    m = re.search(r"^SUMMARY:\s*(.+)$", final_text, re.M)
    summary = m.group(1).strip() if m else step.get("title", "done")
    summary = re.sub(rf"^(Step )?{re.escape(step['id'])}:\s*", "", summary)
    return summary[:72].rsplit(" ", 1)[0] if len(summary) > 72 else summary


# --- one step ------------------------------------------------------------

def load_prompt(plan, step):
    """``file.md`` or ``file.md#R1``: a phase file holds shared sections
    (any ``## `` heading that isn't a step id) and one ``## R1 ...``
    section per step; the step gets the shared text plus its own section."""
    name, _, section = step["prompt"].partition("#")
    text = (pathlib.Path(plan["prompts_dir"]) / name).read_text()
    if not section:
        return text
    parts = re.split(r"(?m)^(?=## )", text)
    is_step = [bool(re.match(r"## [A-Z]+\d+[a-z]?\b", p)) for p in parts]
    shared = "".join(p for p, step_part in zip(parts, is_step) if not step_part)
    mine = [p for p in parts if re.match(rf"## {re.escape(section)}\b", p)]
    if len(mine) != 1:
        raise RuntimeError(f"{name}: expected one section '## {section}', found {len(mine)}")
    return shared.rstrip() + "\n\n" + mine[0]


def run_step(plan, step, git, wt, rundir, sock_dir, stats):
    """Returns (ok, message, pause_reason)."""
    head = git("rev-parse", "HEAD").strip()
    reset_worktree(git, head)
    review = step.get("type") == "review"
    verify_list = qwen_verify_list(plan, step)
    sandbox.prepare(plan, wt, log)
    baseline = 0
    entry = None if review else cached_entry(plan, git)
    cached = entry["passed"] if entry else None
    base_failing = set(entry["failing"]) if entry and entry.get("failing") is not None else None
    if cached is not None:
        baseline = cached
        (rundir / f"{step['id']}-baseline.txt").write_text(
            f"skipped: this tree already passed the same checks ({cached} passed)\n")
        log(f"step {step['id']}: baseline skipped, this tree already passed the checks ({cached} passed)")
    elif not review:
        rc, output = sandbox.verify(plan, wt, verify_commands(plan), plan["verify_timeout_s"])
        (rundir / f"{step['id']}-baseline.txt").write_text(output)
        if rc != 0:
            (rundir / "report.md").write_text(
                f"# Watchdog stopped before step {step['id']}\n\nVerification fails on the clean base, "
                f"so no attempt was started. Output: `{rundir / (step['id'] + '-baseline.txt')}`\n")
            return False, "verification fails on the clean base; fix that first", None
        baseline = count_passed(plan, output)
        base_failing = failing_checks(plan, output)
        remember_verified(plan, git, baseline, output)
    if not review:
        problem = preflight_problem(step, base_failing)
        if problem:
            (rundir / "report.md").write_text(f"# Watchdog stopped before step {step['id']}\n\n{problem}\n")
            return False, f"preflight: {problem}", None
        if base_failing is None:
            log(f"step {step['id']}: no list of the base's failing checks (old cache entry): no preflight")
    task = load_prompt(plan, step) + long_files_note(git, wt, plan, step)
    retry, attempts, last_failure = "", [], None
    max_attempts = step.get("max_attempts", plan["max_attempts"])
    for n in range(1, max_attempts + 1):
        adir = rundir / f"{step['id']}-attempt{n}"
        adir.mkdir(parents=True)
        if review:
            prepare_review(plan, step, git, wt)
            head_text = REVIEW_PREAMBLE.format(diff=REVIEW_INPUT, report=REVIEW_REPORT, paths=PATHS_RULE,
                                               verify="; ".join(f"`{c}`" for c in verify_commands(plan)))
        else:
            note = plan.get("environment_note", "")
            head_text = PREAMBLE.format(allowed=", ".join(f"`{p}`" for p in step["allowed"]),
                                        verify=verify_list, paths=PATHS_RULE,
                                        environment=f" {note}" if note else "")
        prompt = head_text + task + retry
        (adir / "prompt.md").write_text(prompt)
        if len(prompt.encode()) > 120_000:  # Linux limit for one argv string is 128 KiB
            return False, f"prompt too long for the command line ({len(prompt.encode())} bytes)", None
        log(f"step {step['id']} attempt {n}/{max_attempts}: running Qwen")
        ok, reason, st = run_attempt(plan, step, wt, prompt, adir, sock_dir, stats)
        details, changed = "", []
        if ok:
            log(f"step {step['id']} attempt {n}: Qwen finished after {st['minutes']} min, verifying")
            if review:
                ok, reason, details = verify_review(git, wt)
            else:
                ok, reason, details, changed = verify_code(plan, step, git, wt, adir, baseline)
        result = {"time": now(), "step": step["id"], "attempt": n, "ok": ok, "reason": reason, **st}
        (adir / "result.json").write_text(json.dumps(result, indent=2))
        with open(rundir.parent / "log.jsonl", "a") as f:
            f.write(json.dumps({k: v for k, v in result.items() if k != "final_text"}) + "\n")
        attempts.append(result)
        if ok:
            summary = summary_line(step, st["final_text"])
            body = plan.get("commit_body", "Written by Qwen (local, via opencode); verified and committed by the watchdog.")
            if review:
                dest = pathlib.Path(plan["prompts_dir"]) / "reviews" / f"{plan['name']}-{step['id']}.md"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(wt / REVIEW_REPORT, dest)
                git("commit", "-q", "--allow-empty", "-m", f"Step {step['id']}: review ({summary})",
                    "-m", f"Review by Qwen; the report is kept locally ({dest.name}).")
                reset_worktree(git, "HEAD")
                log(f"step {step['id']} review done: {dest}")
                return True, reason, f"review report: {dest}"
            git("add", "-A")
            git("commit", "-q", "-m", f"Step {step['id']}: {summary}", "-m", body)
            log(f"step {step['id']} committed: {summary} ({reason})")
            verified = (adir / "verify.txt").read_text()
            remember_verified(plan, git, count_passed(plan, verified, step), verified)
            touched = [p for p in changed if matches(p, plan["protected"])]
            pause = f"protected files changed ({', '.join(touched)}); review before any image build" if touched else None
            return True, reason, pause
        log(f"step {step['id']} attempt {n} failed: {reason}")
        save_diff(git, adir)
        reset_worktree(git, head)
        verified = (adir / "verify.txt").read_text() if (adir / "verify.txt").exists() else ""
        note = failure_note(plan, step, verified, base_failing) if verified and not review else ""
        failure = (reason, tuple(sorted(n for n in failing_checks(plan, verified) if required_by(step, n))))
        if verified and failure[1] and failure == last_failure and n < max_attempts:
            write_report(rundir, step, attempts)
            return False, (f"step {step['id']}: the same required checks failed twice "
                           f"({', '.join(failure[1][:6])}); stopped instead of attempt {n + 1}"), None
        last_failure = failure
        if details:
            details = (note + "\n\n" if note else "") + f"```\n{details[-4000:]}\n```"
        else:
            details = retry_hint(reason, st, review, plan["context_tokens"]) or "(no details)"
        retry = RETRY.format(reason=reason, details=details)
    write_report(rundir, step, attempts)
    return False, f"step {step['id']} failed {max_attempts} times, last: {attempts[-1]['reason']}", None


def write_report(rundir, step, attempts):
    lines = [f"# Watchdog stopped at step {step['id']} ({step.get('title', '')})", "",
             f"Logs: `{rundir}`", "", "| attempt | minutes | llm requests | completion tokens | result |",
             "|---|---|---|---|---|"]
    for a in attempts:
        llm = a.get("llm", {})
        lines.append(f"| {a['attempt']} | {a.get('minutes')} | {llm.get('requests')} | "
                     f"{llm.get('completion_tokens')} | {a['reason']} |")
    lines += ["", "Each attempt folder has prompt.md, events.jsonl, verify.txt (if verified), "
              "diff.patch (the discarded changes) and opencode-data/ (the session)."]
    (rundir / "report.md").write_text("\n".join(lines) + "\n")


def run_after(plan, step, git, wt, rundir):
    """A step's ``after`` block: host commands that run once the step is
    committed (e.g. regenerate expected outputs with a reference implementation),
    so the plan goes on without a pause. ``{worktree}`` in a command becomes
    the worktree. The commands must sandbox anything from the worktree they
    run themselves (podman, no network); their changes are committed only
    when every changed path matches ``after["allowed"]``, else they are
    discarded and the watchdog pauses. Returns None, or why it pauses."""
    after = step.get("after")
    if not after:
        return None
    head = git("rev-parse", "HEAD").strip()
    for i, cmd in enumerate(after["commands"]):
        cmd = [a.replace("{worktree}", str(wt)) for a in cmd]
        log(f"step {step['id']}: after command {i}: {shlex.join(cmd)[:160]}")
        try:
            r = sh(cmd, check=False, timeout=after.get("timeout_s", 1800))
            out, rc = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired as e:
            out, rc = f"timed out: {e}", 124
        (rundir / f"after-{step['id']}-{i}.txt").write_text(out)
        if rc != 0:
            reset_worktree(git, head)
            return f"after command {i} of step {step['id']} failed (see after-{step['id']}-{i}.txt)"
    paths = changed_paths(git)
    if not paths:
        log(f"step {step['id']}: after commands changed nothing")
        return None
    outside = [p for p in paths if not matches(p, after["allowed"])]
    if outside:
        reset_worktree(git, head)
        return f"after commands of step {step['id']} changed paths outside {after['allowed']}: {outside[:5]}"
    git("add", "-A")
    git("commit", "-q", "-m", f"Step {step['id']}-after: {after.get('title', 'after commands')}",
        "-m", f"Run by the watchdog after step {step['id']}: "
              + "; ".join(shlex.join(c)[:120] for c in after["commands"]))
    log(f"step {step['id']}: after commands committed ({len(paths)} files)")
    return None


def run_checkpoint(plan, wt, rundir, step):
    for i, cmd in enumerate(step.get("checkpoint", plan.get("checkpoint", []))):
        cmd = [a.replace("{worktree}", str(wt)) for a in cmd]
        r = sh(cmd, check=False, timeout=3600)
        (rundir / f"checkpoint-{step['id']}-{i}.txt").write_text(r.stdout + r.stderr)
        if r.returncode != 0:
            return f"checkpoint command {i} failed (see checkpoint-{step['id']}-{i}.txt)"
    return None


# --- main ----------------------------------------------------------------

def take_lock(lock):
    """One watchdog per plan. A lock whose process is gone (killed, or an old
    version without the SIGTERM handler) is stale and replaced; observed: a
    stale lock made every later start fail at once."""
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                os.kill(int(lock.read_text().strip()), 0)
            except (ValueError, ProcessLookupError):
                log(f"removing the stale lock {lock}")
                lock.unlink(missing_ok=True)
                continue
            except PermissionError:
                pass
            sys.exit(f"another watchdog for this plan runs (pid in {lock})")
    sys.exit(f"could not take the lock {lock}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("--dry-run", action="store_true", help="show the next step and its prompt, run nothing")
    ap.add_argument("--once", action="store_true", help="stop after one step")
    args = ap.parse_args()
    plan = load_plan(args.plan)

    lock = take_lock(HERE / f".{plan['name']}.lock")
    # `systemctl stop` sends SIGTERM: leave through `finally` (lock removed, proxy down)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    proxy = None
    try:
        wt = pathlib.Path(plan["worktree"])
        if args.dry_run and not wt.exists():  # no worktree yet: no step is done; create nothing
            step = plan["steps"][0]
            print(f"next step: {step['id']} ({step.get('title', '')}), allowed: {step.get('allowed')}\n")
            print(load_prompt(plan, step))
            return
        wt = ensure_worktree(plan)
        git = Git(wt)
        rundir = HERE / "runs" / plan["name"] / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        while True:
            done = done_steps(plan, git)
            todo = [s for s in plan["steps"] if s["id"] not in done]
            if not todo:
                notify("local-llm-harness", f"plan {plan['name']}: all steps done")
                return
            step = todo[0]
            if args.dry_run:
                print(f"next step: {step['id']} ({step.get('title', '')}), allowed: {step.get('allowed')}\n")
                print(load_prompt(plan, step))
                return
            rundir.mkdir(parents=True, exist_ok=True)
            if proxy is None:
                sock_dir = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "local-llm-harness" / plan["name"]
                sock_dir.mkdir(parents=True, exist_ok=True)
                stats, proxy = llmproxy.start(sock_dir / "llm.sock", rundir / "llm-requests.jsonl",
                                              KEY_FILE.read_text().strip(),
                                              budget=plan.get("reasoning_budget"),
                                              drop_old_thinking=plan.get("drop_old_thinking", False))
            ok, msg, pause = run_step(plan, step, git, wt, rundir, sock_dir, stats)
            if not ok:
                notify("local-llm-harness stopped", f"{msg}. Report: {rundir / 'report.md'}")
                sys.exit(1)
            if not pause:
                pause = run_after(plan, step, git, wt, rundir)
            if step.get("pause_after") or pause:
                failed = run_checkpoint(plan, wt, rundir, step) if step.get("pause_after") else None
                why = "; ".join(x for x in (pause, failed) if x) or "checkpoint"
                notify("local-llm-harness paused", f"after step {step['id']}: {why}. Logs: {rundir}")
                return
            if args.once:
                return
    finally:
        if proxy is not None:
            proxy.shutdown()
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
