#!/usr/bin/env python3
"""Drop a plan's failed step so the plan can go on with the next one.

The failed step is the first one without a `Step <id>:` commit on the plan's
branch. Prints its id and rewrites the plan without it (the first run keeps
a copy as <plan>.orig: restore it before the plan's next real run); exits 1
when no step is left or the failed step is in the plan's "keep" list (or
given as an argument): later steps depend on it.
    python3 skip_step.py plans/PLAN.json [KEEP_ID ...]
"""
import json
import pathlib
import shutil
import subprocess
import sys


def main(path, keep):
    """Skip the failed step of the plan at path unless it is in keep."""
    plan = json.loads(pathlib.Path(path).read_text())
    log = subprocess.run(["git", "-C", plan["repo"], "log", f"{plan['base']}..{plan['branch']}", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    done = {s.split(":", 1)[0].removeprefix("Step ").strip() for s in log.splitlines() if s.startswith("Step ")}
    left = [s for s in plan["steps"] if s["id"] not in done]
    if not left or left[0]["id"] in keep:
        return 1
    orig = pathlib.Path(path + ".orig")
    if not orig.exists():
        shutil.copy(path, orig)
    plan["steps"] = [s for s in plan["steps"] if s is not left[0]]
    pathlib.Path(path).write_text(json.dumps(plan, indent=1))
    print(left[0]["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], set(sys.argv[2:]) | set(json.loads(pathlib.Path(sys.argv[1]).read_text()).get("keep", []))))
