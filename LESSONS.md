# Lessons: a local model writing code unattended

From several weeks of real use: Qwen3.8-27B (Q5_K_M, llama.cpp, 131k context, 16k output tokens per reply), driven by opencode inside this watchdog's sandbox, on one project (a Rust backend, a plain-JS web page, Python test harnesses), planned and reviewed by a stronger model (Claude). Numbers are from the watchdog's logs. Each rule says what happened; the fix is in the code or in how steps are written. It is one model on one project with one planner: take the numbers as examples, not as targets.

## Start here

**The idea.** A strong model (the *planner*, here Claude) splits the work into small steps and makes every decision. A local model (here Qwen) carries out one step at a time in a locked-down container. The watchdog runs each attempt, checks the result itself, and commits it or retries. The model never verifies or commits its own work.

**Five takeaways**

1. **The model executes; it does not explore.** It has no network, no documentation, no search and a limited context. Whatever a step needs to know, the planner finds out and writes down ([the roles](#the-roles-an-architect-plans-the-model-executes)).
2. **Most failures are the output limit, and the thinking causes them**: 26 of 41 failed attempts. A prompt rule to think less was ignored; a thinking budget enforced per request fixed it ([why steps fail](#why-steps-fail)).
3. **Enforce in code, not in the prompt.** Prompt rules hold early in a session and fade ([holding a model to a role](#holding-a-model-to-a-role)).
4. **The context is the scarce resource.** Give a reading list, block the files it must not read, drop old thinking from the history ([writing steps](#writing-steps-the-planners-checklist)).
5. **The harness verifies and commits, never the model.** After a failure it tells the next attempt what went wrong and what to do instead; the same failure twice stops the step ([the harness](#the-harness-what-code-enforces)).

**The core every harness needs** (this one has all of it; plan keys in the [README](README.md)):

| Area | What |
|---|---|
| Isolation | A sandbox container: no network, read-only root, no capabilities, no git. The API key stays outside, behind the proxy. |
| Proxy | A thinking budget per request (`reasoning_budget`, 4096), old thinking dropped (`drop_old_thinking`), images replaced with a note, every request logged. |
| Context | The agent's declared context below the server's, a fresh session per attempt, files it must not read refused by the tool (`no_read`), an attempt stopped after N summaries without an edit (`max_summaries_without_edit`). |
| Verification | A fresh verify container: only allowed paths changed, every check run, passed tests counted only from the plan's own checks, each check's output with its stderr. The watchdog commits. |
| Retries | A specific note for the next attempt, a stop after the same required failure twice, the rejected attempt's diff kept. |
| Start | The step's checks run on the clean base first (preflight, unless the step `fixes` them); a lock whose process is gone doesn't block a start. |
| Step prompts | What is decided, a reading list, a never-read list, "write the first file within your first ten tool calls", no `kill` ([the example](examples/hello/prompts/hello-steps.md)). |

Only when needed: a file size limit, planted faults, a helper that starts and stops servers, separate scopes under systemd, a grammar or JSON schema, several agents.

**Words used here**

- **Thinking**: the reasoning a model writes before its answer. It counts against the output limit and, kept in the history, fills the context.
- **Output limit**: the most tokens one reply may have (16k here). A reply that hits it ends, often with nothing written.
- **Context**: everything the model sees in one request: the prompt, the history, the files it read.
- **Session summary**: when the context is nearly full, the agent replaces the history with a summary and goes on.
- **opencode**: the coding agent (read, edit, shell) that sends the model's requests and runs its tools.
- **Plan, step, attempt**: a plan is a list of steps; a step gets a few attempts, each in a fresh sandbox.
- **Check, verify stage**: a command the watchdog runs after an attempt, e.g. the test suite. **Baseline**: the passing tests before the step; a step may not lower their number.
- **Allowed paths**: the files a step may change. **Preflight**: running the step's checks on the clean base before the first attempt.
- **Goldens**: recorded expected outputs that tests compare against. **Seam**: the place in existing code where new code plugs in. **Planted faults**: deliberately broken copies of the code that the checks must catch.

## The roles: an architect plans, the model executes

The model in the sandbox has no information sources: no network, no documentation, no search, a limited context. So it must never have to find a solution. The planner works like an architect: for every step it gathers what the step needs (the code's seams, library and tool behaviour, the environment's timing and state, the data), analyzes it, makes every design decision, and adapts the harness where a rule can be enforced instead of written. The model then executes a decided task. Most of the expensive failures below were the model discovering something it had no source for; once the planner had found and written down the answer, the same steps passed in 10–20 minutes.

## The numbers

The first 94 steps: 73 passed on the first attempt (78 %), 88 in the end, 129 attempts. Median time of a passing step: 10–33 minutes depending on the kind of work. Later plans with fully decided steps did better: in one, every step's first attempt was correct (8 steps, under two hours in all; one was wrongly rejected by a harness bug, see "count passed tests" below).

Failed attempts by reason: **output limit or full context 26**, files outside the allowed paths 5, agent crashes 4, timeouts 3, line limit 2, fewer passing tests 1. Every step that failed three times had at least one output-limit stop.

## Why steps fail

1. **Drafting in the thinking.** The typical failure: the model plans a whole module (or a README, or a report) as code in its thinking and hits the output limit with no file written. A prompt rule ("keep your thinking short") was in every prompt and was ignored. Triggers: questions the prompt left open, design freedom, sizing work. **Fix: a thinking budget per request** (see below), and decisions made before the step.
2. **Reading everything.** Without a reading list it explores broadly and re-reads: one step read 52 files, another read a forbidden 1700-line harness file 38 times. The context fills, or the agent's session summary runs out of output, and it reads the same files again after each summary. **Fix: a reading list with line ranges, `no_read` globs enforced by the tool, and stopping an attempt after N summaries without an edit.**
3. **A design task disguised as a fill-in task.** "Run the existing checks on a second browser" failed six times: the model had to find the seam in a 5,000-line harness and ran out of context exploring it. Split into "add these five methods here (the refactor), then a driver with this interface, then the runner" with the seam spelled out, every part passed within minutes. **The planner makes the design decision and writes it down; the model implements it.**
4. **Open decisions near hard limits.** A whole attempt was spent deliberating whether code fits into a file 6 lines under the size limit. A limit plus an undecided layout makes it deliberate instead of write.
5. **Big single writes.** Writing a whole file in one tool call cut the call off mid-JSON (the server answers 500). Writing in parts fixed it.
6. **Images.** The model is text-only; opening its own screenshots made every following request fail. The proxy now replaces image parts with a note.
7. **The allowed list.** Five attempts touched a file the step needed but didn't allow (mostly test files). Build the list from a real build of all targets, not from memory.
8. **Fixing only the spot the test points at.** After learning a convention it fixed one place and left the same bug elsewhere; asking explicitly to check the other places worked.

## Writing steps (the planner's checklist)

- **One feature per step**; split pure logic, storage and interface. Expect 10–35 minutes per step.
- **"What is decided (verified)"**: file and function names, placement, line budgets of files near a limit, the order of checks, null vs missing, library behaviour (tried in the sandbox first). Every open question costs thinking tokens.
- **A reading list** with line ranges, and a **never-read** list. "Write the first file within your first ten tool calls."
- **Facts, not judgement**: expected values as a table, measured sizes and colours for layout work ("change this value, run this check"), the data a test must fake (paths, the place, the clock).
- **Checks first, seen failing, proven passable** (a throwaway implementation outside the repo), and **satisfiable at that point in the plan**: run them on the base before handing the plan over.
- **No reference code** if the model's work is meant to count; behaviour, facts and tests instead. With the code in the prompt, the planner did the work (and paid for it), and the model only copies.
- **Resolve spec conflicts in the prompt.** "A comment on every public item" and "no file over 400 lines" collide near the limit; say how to balance them.
- **A size limit needs room.** It applies to code, never to data files that only grow; a file at the limit is split before the plan, not by the model during it (one night lost three attempts to "401 lines").
- **Test steps get a check that counts what the tests catch.** A mutation tool (e.g. cargo-mutants) finds code changes no test notices; the step's gate applies exactly those changes to a copy and fails while the tests still pass. And a test must not take its expected value from the constant it guards (a test of a size cap that uses the cap constant follows a mutated cap): write the number.
- **Name the sample data.** A test needs example data (a place, a station id, a user); left open, the model picks something from what it has read, once a station from a private test location. The prompt names the public fixtures to take it from. Before a push, scan what is about to become public (the local commits), not the history that already is.
- **A change to an input re-records its outputs.** If a step changes files that generated expectations depend on (byte hashes, goldens), allow those outputs and say how to regenerate them, or the step can't pass.
- **New infrastructure needs its rules written down.** A new browser driver returned before the page's requests finished and leaked storage between pages; the model spent 75 minutes tuning waits. A five-minute diagnosis by the planner and two sentences in the prompt fixed it.

## The harness (what code enforces)

- **What can be counted is enforced by code**; what can't (design, review quality) goes to a separate agent with a checklist; prompts describe the role but don't enforce it.
- **A thinking budget per request** got a step through that failed 3 of 3 without one (4096: passed on the first attempt). The model fills whatever budget it gets, so bigger isn't better.
- **Drop old thinking from the history.** llama-server keeps earlier thinking by default; with 4–8k of thinking per reply the context fills in minutes.
- **The agent's declared context stays below the server's** (100k vs 131k), so its session summary still fits.
- **Specific retry notes.** After a failure the next attempt is told what went wrong and what to do instead, and whose failure it is: "failing because of your change", "this step must make it pass", "not required, ignore". A note that names the way out ("put it into the new file") works better than one that names the symptom. Without these, a model chases whatever the output shows.
- **The same required failure twice: stop**, don't spend a third attempt.
- **Preflight on the clean base.** A plan once started on a base whose expected outputs were stale; the first attempt was correct but every check failed in a part it couldn't change, and the next two attempts (2.5 h) chased it. Now a check the step requires that fails on the base stops the plan, unless the step declares that it `fixes` it.
- **Run every verify stage**, also after one failed: one failing stage must not hide the others.
- **Count passed tests only from the plan's own checks.** A step's extra gate printed "17 passed"; counted in, it raised the next step's baseline and a correct step failed "fewer passing tests".
- **Keep each check's output under its own heading.** The verify script returned stdout, then stderr; `unittest` reports on stderr, so its "Ran 3 tests" landed under the last command and a correct step counted 0. `exec 2>&1` keeps both streams in order.
- **Never let the model stop processes in its own container.** Its shell shares the agent's process group; a broad `kill` ended the agent itself, three times in one night. Give it a helper that starts and stops what it needs in separate process groups (not part of this harness: write one when steps start servers), and forbid kills in the prompt.
- **Locks must survive a kill.** A watchdog stopped by the service manager left its lock; every later start failed at once, and a "skip the failed step" script took that for a failing step. Check whether the lock's process still lives; never blame a step for a run that ended within seconds.
- **A container started from inside a service unit dies with the unit**; start long-lived ones in their own scope.
- **A plan that continues an existing branch needs the start commit as `base`, never the branch name**: done steps are the commits in `base..branch`, so a base that moves with the branch made the watchdog start the committed step again and crash on its attempt folder. The watchdog now refuses such a plan.

## Operating it

- **Salvage before you retry.** Twice in two days a discarded attempt was the right one (once only the test count was wrong, once two files were a few lines over a limit). Check the saved diff before re-running a step.
- **Read the logs before changing anything.** "Output limit" had three different causes: drafting in the thinking, a full context, a cut-off session summary.
- **Measure first.** Profiling the slow test suite showed it waited rather than computed; parallel jobs took it from 12 to under 2 minutes.
- **Planted faults keep checks honest**: deliberately broken copies that the checks must catch prove a faster or rewritten harness didn't quietly weaken them.
- **Spend the planner's effort where it pays twice**: decisions before a step, reading lists, measured facts, and harness fixes every later step benefits from. Prefer unattended runs that report back over watching.
- **Never weaken a security gate to save approvals** or time; reduce how often the slow command runs instead.

## Holding a model to a role

Can it keep its own thinking short? Not reliably: it can't see its token count or how much it has read, long thinking comes exactly when it is unsure, and one more read always looks justified. A second model as supervisor shares the blind spots. Methods, most reliable first:

1. **Limits at generation time**: thinking budget, thinking off for fully decided steps, a grammar or JSON schema where a format is the contract, `max_tokens`.
2. **The environment**: tools per role, allowed paths, the sandbox, checks as the judge, budgets enforced by the proxy and watchdog.
3. **The context**: a fresh session per role, exactly the material needed, decisions made beforehand.
4. **Training** (a LoRA on successful runs): strongest for staying in a role, most effort, untried here.
5. **Prompt rules alone**: they hold early in a session and fade.

## Several agents

- **A hop between agents costs a prompt re-read** (here ~3.5k tokens/s: a 50k context ≈ 15 s) and evicts other agents' caches. One GPU gives isolation, not parallel speed.
- **Tools without an LLM behind them** (via MCP or the harness): run the step's checks and return only the failure lines, return only the lines a step may read, verify and commit owned by the harness. The agent can do only what a tool offers.
- **An LLM behind a tool pays off only when** it compresses (reads 2,000 lines, returns 30), independence matters (a review in a fresh context found bugs the checks missed), or the work is at least ~10× the switch cost.
- **Reviews**: a checklist and "quote the line or it isn't a finding": no invented findings, and real bugs found.

## llama-server knobs

- `--reasoning-budget N` and `--reasoning-budget-message MSG` (or per request: `reasoning_budget_tokens`, `reasoning_budget_message`, which is what the proxy sets, so the server stays shared).
- `--reasoning on|off|auto`: thinking off for decided steps (untested here).
- `--reasoning-preserve` is on by default (old thinking stays in the history); the proxy's `drop_old_thinking` removes it per request.
- `--grammar`, `--json-schema`: force an output format.
- Thinking lengths seen in 3,827 normal replies: median 113 tokens, p90 1,540, p95 3,162, p99 8,280. A 4096 budget cuts 3.5 % of replies.

## Open questions

- Thinking off for fully decided steps: faster, or worse?
- How much design freedom works without measured facts for every case.
- Whether a LoRA on the successful runs keeps the model in its role better than any prompt.
