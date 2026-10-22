"""The sandbox container Qwen works in, and the fresh container that verifies.

Isolation instead of approval: Qwen gets a shell, but only inside a
container with

* the worktree at /work (read-write) and its `.git` pointer file mounted
  read-only on top (there is no git in the image, and the gitdir isn't
  mounted), no symlink to anything outside survives verification;
* no network (`--network=none`); the only way out is the llm proxy socket
  at /run/llm/llm.sock (chat completions only, the API key is added
  outside);
* dependencies only from what the plan's ``prepare`` commands fetched on
  the host (with network, outside the sandbox) into a cache that is
  mounted read-only (``sandbox.mounts``);
* the plan's ``material`` folder, if any, at /material (read-only):
  reference files the steps copy from;
* uid 1000, read-only root filesystem, all capabilities dropped,
  no-new-privileges, CPU/memory/pids limits;
* opencode's config mounted read-only (its directory is a tmpfs because
  opencode writes a service.json next to it).

The verification container uses the same image with the worktree
read-only and its own volumes, which Qwen can't reach.

The plan's ``sandbox`` block: ``image`` (required), ``cpus``, ``memory``,
``pids``, ``env`` (both containers), ``agent_env`` (Qwen's container only),
``mounts`` (``"HOST:CONTAINER[:ro]"``, ``{cache}`` and ``{here}`` filled in),
``volumes`` (container paths backed by a named volume per plan and
container, e.g. a build directory that survives attempts), ``tmpfs``.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shlex
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
OPENCODE_BIN = os.environ.get("WATCHDOG_OPENCODE_BIN", "/usr/bin/opencode")


def _run(args, timeout=None, check=True):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=check)


def cache_dir(plan) -> pathlib.Path:
    return HERE / "cache" / plan["name"]


def prepare(plan, wt: pathlib.Path, log) -> None:
    """The plan's ``prepare`` block: host commands that fetch what the
    offline sandbox needs (e.g. vendored dependencies) into the plan's cache
    folder, run again only when one of the ``inputs`` (worktree paths, e.g. a
    lock file) changes. ``{worktree}`` and ``{cache}`` are filled in. The
    commands run on the host with network, so they must not run code from the
    worktree outside a container (vendor, don't build)."""
    block = plan.get("prepare")
    if not block:
        return
    cache = cache_dir(plan)
    cache.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    for rel in block.get("inputs", []):
        f = wt / rel
        digest.update(rel.encode() + b"\0" + (f.read_bytes() if f.is_file() else b"<missing>"))
    marker = cache / ".inputs-hash"
    if marker.exists() and marker.read_text() == digest.hexdigest():
        return
    for cmd in block["commands"]:
        cmd = [a.replace("{worktree}", str(wt)).replace("{cache}", str(cache)) for a in cmd]
        log(f"prepare: {shlex.join(cmd)[:160]}")
        _run(cmd, timeout=block.get("timeout_s", 1800))
    marker.write_text(digest.hexdigest())


def _common(plan, role):
    sb = plan["sandbox"]
    cpus = sb.get("cpus", max(1, (os.cpu_count() or 2) - 2))
    args = [
        "--init", "--network=none", "--read-only",
        "--tmpfs", "/tmp:size=4g",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--userns=keep-id", "--user", "1000:1000",
        "--cpus", str(cpus), "--memory", sb.get("memory", "16g"),
        "--pids-limit", str(sb.get("pids", 4096)),
        "-e", "HOME=/tmp",
    ]
    for path in sb.get("tmpfs", []):
        args += ["--tmpfs", path]
    for k, v in sb.get("env", {}).items():
        args += ["-e", f"{k}={v}"]
    for m in sb.get("mounts", []):
        args += ["-v", m.replace("{cache}", str(cache_dir(plan))).replace("{here}", str(HERE))]
    for i, path in enumerate(sb.get("volumes", [])):
        args += ["-v", f"wd-{plan['name']}-{role}-{i}:{path}"]
    return args


def opencode_config(plan, adir: pathlib.Path) -> pathlib.Path:
    """The sandbox's opencode.json; with the plan's ``no_read`` globs (matched
    against the absolute path, e.g. ``*tests/harness.py``) a copy for this
    attempt in which the read tool refuses those files. A prompt rule alone
    wasn't enough: one attempt read a forbidden 1700-line file 38 times."""
    base = HERE / "sandbox" / "xdg" / "opencode" / "opencode.json"
    if not plan.get("no_read"):
        return base
    cfg = json.loads(base.read_text())
    cfg["permission"]["read"] = {"*": "allow", **{glob: "deny" for glob in plan["no_read"]}}
    path = adir / "opencode.json"
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return path


def start(plan, wt: pathlib.Path, name: str, sock_dir: pathlib.Path, oc_data: pathlib.Path) -> str:
    """Start Qwen's sandbox (the entrypoint is the llm forwarder)."""
    _run(["podman", "rm", "-f", "-t", "0", name], check=False)
    config = opencode_config(plan, oc_data.parent)
    agent_env = [a for k, v in plan["sandbox"].get("agent_env", {}).items() for a in ("-e", f"{k}={v}")]
    args = ["podman", "run", "-d", "--name", name, *_common(plan, "agent"),
            "-e", "XDG_CONFIG_HOME=/tmp/config", "-e", "XDG_DATA_HOME=/oc-data",
            "-e", "XDG_STATE_HOME=/tmp/state", "-e", "XDG_CACHE_HOME=/tmp/cache",
            *agent_env,
            "-v", f"{wt}:/work", "-v", f"{wt / '.git'}:/work/.git:ro",
            # the plan's reference files (decided code, prototypes) for the steps to copy from;
            # Qwen's sandbox only: the verification never sees them
            *(["-v", f"{plan['material']}:/material:ro"] if plan.get("material") else []),
            "-v", f"{sock_dir}:/run/llm",
            "-v", f"{OPENCODE_BIN}:/usr/local/bin/opencode:ro",
            "-v", f"{config}:/tmp/config/opencode/opencode.json:ro",
            "-v", f"{oc_data}:/oc-data",
            plan["sandbox"]["image"]]
    _run(args, timeout=120)
    # opencode writes service.json next to its (read-only) config file
    _run(["podman", "exec", "--user", "0", name, "chmod", "1777", "/tmp/config", "/tmp/config/opencode"])
    return name


def opencode_cmd(name: str, prompt: str, title: str, model: str | None) -> list[str]:
    cmd = ["podman", "exec", "-w", "/work", "-e", "PWD=/work", name,
           "opencode", "run", "--format", "json", "--title", title]
    if model:
        cmd += ["--model", model]
    return cmd + [prompt]


def stop(name: str) -> None:
    _run(["podman", "rm", "-f", "-t", "0", name], check=False, timeout=60)


def verify(plan, wt: pathlib.Path, commands: list[str], timeout: int):
    """Run the verification commands in a fresh container (worktree
    read-only, its own volumes), all of them even when one fails.
    Returns (returncode, output); nonzero when any command failed."""
    # every command runs, also after one failed: the output shows every stage's
    # result (one failing stage must not hide the others'). stderr joins stdout
    # in order: each command's output must stay under its own `$ <command>` line
    # (unittest reports on stderr; appended at the end, its count landed in the
    # last command's section and the step "lost" its tests)
    script = "exec 2>&1\nrc=0\n" + "\n".join(
        f"echo {shlex.quote('$ ' + c)}\n( set -e; {c} ) || {{ r=$?; echo \"[this command failed: exit $r]\"; rc=1; }}"
        for c in commands) + "\nexit $rc"
    args = ["podman", "run", "--rm", *_common(plan, "verify"),
            "-v", f"{wt}:/work:ro", "-w", "/work", "--entrypoint", "bash",
            plan["sandbox"]["image"], "-c", script]
    try:
        r = _run(args, timeout=timeout, check=False)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return 124, f"verification timed out after {timeout} s: {e}"
