# Local coding agent for a Legion 5i (RTX 4070 Laptop, 8 GB)

A Windows 11 setup that runs a coding agent entirely on the laptop. [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` serves a GGUF model with MoE expert weights in system RAM. A small Python harness talks to the server's OpenAI-compatible API, edits the target repo, runs tests, and commits the result.

Nothing in this repository calls a cloud model API. Model weights are downloaded from Hugging Face and are not committed here.

The agent code is MIT licensed. The models keep their own licenses (Apache 2.0 for the three profiles below).

## First commands

Run these in **Windows PowerShell** or Windows Terminal on the Windows host. Do not run them inside WSL.

```powershell
cd C:\src\local-ai-4070
Set-ExecutionPolicy -Scope Process Bypass

# Driver check, Python venv, llama.cpp CUDA build, quality + day models.
.\scripts\setup.ps1

# Optional, once: find the fastest offload that stays under 8 GB with 1 GB free.
.\scripts\tune-n-cpu-moe.ps1 -Profile quality

# Serve the overnight model. Leave this window open.
.\scripts\start-server.ps1 -Profile quality
```

In a second window:

```powershell
cd C:\src\local-ai-4070
Set-ExecutionPolicy -Scope Process Bypass

# No GPU. Proves the harness, git commit, and pytest loop.
.\scripts\smoke-test.ps1

# Real model, throwaway repo, after the server is healthy.
.\scripts\smoke-test.ps1 -Live -Profile quality

# Overnight task against a clean git repo.
.\scripts\overnight.ps1 -Task .\tasks\smoke-task.md -Project C:\src\my-app -Profile quality

# A folder of ordered tasks, unattended. Passing branches merge back into the project branch.
# Check out an integration branch in the target repo first (not main, unless you pass -AllowMain).
python -m local_agent validate C:\src\my-app\tasks --project C:\src\my-app
.\scripts\queue.ps1 -TaskDir C:\src\my-app\tasks -Project C:\src\my-app -Profile quality
```

Plug the laptop in before `setup.ps1` downloads anything large, and leave it plugged in for every server and overnight run.

## What changed / how to run a long session

This pass is about a run you can leave overnight. Sampling (temperature, top-p, top-k, repeat penalty) is unchanged.

- `quality` context is 65536. `--reasoning-budget` is 10240 so a thinking trace cannot consume the whole 16384 `max_tokens` and come back empty. Both are overridable in `runtime\overrides.json`.
- The prompt budget is capped at 32768 tokens (it was about 24000 on the old 40K window). The review diff cap is 32000 characters.
- Qwen profiles send `--chat-template-kwargs {"preserve_thinking":true}` and replay `reasoning_content` for the last 2 assistant turns in a phase. Older reasoning is dropped before tool output is trimmed. `day` (gpt-oss) does not replay reasoning.
- Optional `smoke_command` runs after the tests. `smoke_mode: launch` starts a GUI or a server, waits (default 6 seconds), treats "still running, no traceback" as a pass, and kills the process tree.
- `test_first: true` / `--test-first` / `-TestFirst` writes tests first and locks them. Default off: a bad locked test cannot be repaired in the fix loop.
- The queue retries once (`-Retries 1`, `--retries 1`) from a fresh `agent/<slug>-r2` branch when the iteration budget, a loop, or the review fails. A single overnight run does not retry unless you pass `-Retries`. The stop file is checked between attempts. Archive only happens when an attempt passes.
- `python -m local_agent eval --mock` checks the fixture checkers with no server. `--profile quality` runs them against the live server.
- Failed runs that only explored, leaked `<tool_call>` into the review, or let review-fix destroy a passing tree are covered in [Guards from failed runs](#guards-from-failed-runs). Defaults are unchanged for an existing task file: plan 8 turns, implement 30, exploration cap 10, no bonus turns.

On the laptop, after you pull, before the long queue:

```powershell
# Stop llama-server first. Re-tune: the context is 64K now, so the old VRAM winner may not fit.
.\scripts\tune-n-cpu-moe.ps1 -Profile quality

# One load of the current offload. Does not write overrides. memory.used should stay ~1 GB under 8 GB.
.\scripts\tune-n-cpu-moe.ps1 -Profile quality -Check

.\scripts\start-server.ps1 -Profile quality
.\scripts\smoke-test.ps1 -Live -Profile quality

# Plumbing only, no GPU. Then the live scoreboard if you want a longer signal.
python -m local_agent eval --mock
python -m local_agent eval --profile quality

python -m local_agent validate C:\src\my-app\tasks --project C:\src\my-app
.\scripts\queue.ps1 -TaskDir C:\src\my-app\tasks -Project C:\src\my-app -Profile quality
```

Put `smoke_command` on GUI tasks (`smoke_mode: launch`). Leave `-TestFirst` off unless the task asks for it. `-Retries 1` is already the queue default. Create `runtime\STOP_QUEUE` from another window (`.\scripts\stop-queue.ps1`) to finish the current attempt and start nothing else.

To install the toolchain without the weights (about 1 GB instead of about 35 GB):

```powershell
.\scripts\setup.ps1 -SkipModels
.\scripts\smoke-test.ps1
```

## Hardware this is aimed at

| | |
| --- | --- |
| Machine | Lenovo Legion 5i |
| OS | Windows 11, native. Not WSL. |
| CPU | Intel Core i7 |
| GPU | NVIDIA GeForce RTX 4070 Laptop, 8 GB GDDR6 |
| RAM | 32 GB |

WSL is the wrong place for these models. A community 8 GB bench ([witcheer/local-agentic-coding-bench-8gb-vram-2026-05](https://huggingface.co/datasets/witcheer/local-agentic-coding-bench-8gb-vram-2026-05)) ran out of memory on the 35B MoE model because WSL's RAM cap is smaller than the 32 GB the laptop actually has. `scripts\setup.ps1` refuses to continue when it sees `WSL_DISTRO_NAME`.

Run one model at a time. Both profiles listen on `127.0.0.1:8080`, and an 8 GB card cannot hold two of them.

## Why a small Python harness

The same bench's reliable setup was gpt-oss-20b plus a coding agent whose tools were only read, write, edit, and shell (they used [Pi](https://github.com/badlogic/pi-mono)). 9B models produced a file and then broke on the next structured edit. Qwen3.6-35B-A3B did not break tool calls, but thinking tokens and a 32K window made multi-step edits too slow or ran out of context.

This repo keeps that tool surface and does not vendor Pi or turn on llama.cpp's experimental `--agent` tools.

- The tool schemas are four flat functions. Nested JSON is where local models drop required fields.
- Every shell command has a timeout. A blocking command (`tail -f`, a server, a prompt) otherwise hangs the run until morning.
- The same tool call three times gets a warning. The fourth identical call, or an A-B-A-B oscillation, ends the phase.
- Old turns are dropped as whole tool-call groups so the prompt stays inside the context window. A tool result is never kept without its call.
- Thinking text is logged. Qwen profiles send `reasoning_content` back for the last two assistant turns in the phase. Older reasoning is dropped before tool output is trimmed. gpt-oss does not get reasoning replay; its template does not use that field.
- If a turn comes back empty because `finish_reason` is `length`, the harness retries once at the profile's higher token ceiling, then nudges the model once. Thinking models spend the budget inside the trace and return no `content` when `max_tokens` is small.
- The overnight runner, not the model, executes the test command, writes `AGENT_REPORT.md`, and commits. The model is told not to `git commit` or `git push`.

The harness uses the Python standard library only. Tests mock the HTTP API and also run one scripted overnight loop that really executes pytest and git. See [What was not run on an RTX 4070](#what-was-not-run-on-an-rtx-4070).

## Models

Checked on 2026-11-10 against Hugging Face (`x-linked-size`) and the llama.cpp server README at binary tag `b12110` (the build attached to release `v0.9.0`). `scripts\setup.ps1` resolves the latest Windows CUDA release itself; it does not pin `b12110`.

| Profile | When to use | Weights | File size | Context | Offload | Generation budget |
| --- | --- | --- | --- | --- | --- | --- |
| `quality` | Overnight. Accuracy over speed. | [`unsloth/Qwen3.6-35B-A3B-GGUF`](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF) `Qwen3.6-35B-A3B-UD-Q4_K_M.gguf` | 22,134,528,992 bytes (22.1 GB) | 65536 | `--n-cpu-moe 34` | 16384, retry 24576, reasoning budget 10240 |
| `day` | Daytime agent loops. The bench's reliable pick. | [`ggml-org/gpt-oss-20b-GGUF`](https://huggingface.co/ggml-org/gpt-oss-20b-GGUF) `gpt-oss-20b-MXFP4.gguf` | 12,109,566,624 bytes (12.1 GB) | 32768 | `--n-cpu-moe 30` | 8192, retry 16384 |
| `max` | Optional. Dense 27B, slow. | [`ggml-org/Qwen3.8-27B-GGUF`](https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF) `Qwen3.8-27B-Q4_K_M.gguf` | 19,403,684,480 bytes (19.4 GB) | 16384 | `--n-cpu-ffn 64` | 16384, retry 24576 |

`quality` is Qwen3.6-35B-A3B: 35B total, about 3B active, MoE. The Unsloth UD-Q4_K_M file is the one to download. A published tensor breakdown of that file describes 40 layers and about 0.45 GiB of expert weights per layer. Shipped `--n-cpu-moe 34` keeps 34 of those layers' experts in RAM. The bench used 30 on a desktop RTX 4060 Ti and recorded 7.2 GB, which leaves too little room once Windows is also using a laptop GPU.

`day` is gpt-oss-20b in the native MXFP4 GGUF from ggml-org. Do not re-quantize it to Q4_K_M; the experts already are 4-bit. The model has 24 layers, so `--n-cpu-moe 30` means every expert block stays in RAM. That is the setting the bench used (`--jinja`, `ncmoe=30`, `-c 32768`, `-n 8192`) when it completed 10/10 agent tasks at about 1.8 GB of VRAM. This profile passes `--n-predict -1` and lets the request's `max_tokens` be the cap, so a thinking trace is not cut off by a server-side 8192 limit. The request budget is still 8192, with one retry at 16384.

`max` is dense Qwen3.8-27B Q4_K_M. There is no expert offload. `--n-cpu-ffn 64` keeps every feed-forward block in RAM and leaves attention on the GPU. The official ggml-org repo has Q4_K_M, Q8_0, and BF16 for this model, not a Q3. If the machine swaps, lower `context` in an override before you go looking for a smaller quant. Download it only when you ask:

```powershell
.\scripts\download-models.ps1 -Models max
.\scripts\start-server.ps1 -Profile max
```

Do not point the harness at a 7B-9B "coder" model. In that bench they finished a one-file task and then failed the multi-step edit, usually by emitting tool JSON that dropped a required field.

### Flags every profile shares

These are built by `python -m local_agent server-args` and passed through by `start-server.ps1`. A value that is a JSON object is handed over as a `LLAMA_ARG_*` environment variable (`server-args --format launch`). Windows PowerShell 5.1 strips the double quotes inside a native argument, and llama-server then cannot parse the JSON.

- `--host 127.0.0.1` so the API is not on the network. There is no API key because nothing outside the machine should be able to reach it.
- `--n-gpu-layers all` so attention (and, for MoE, the non-expert weights) can sit on the GPU. Expert or FFN placement is the other flag.
- `--parallel 1` so the KV cache is not allocated more than once.
- `--fit off`. llama.cpp 0.9 will otherwise change unset values to fit VRAM, including shrinking the context. The context above is intentional. Fit is how a 40K agent loop silently becomes a 4K one.
- `--jinja`. Tool calling uses the chat template stored in the GGUF. Current llama-server builds default this on; the flag is still passed.
- `--flash-attn on` and `--cache-type-k q8_0 --cache-type-v q8_0`. Quantized KV needs flash attention on CUDA.
- `--batch-size 512 --ubatch-size 512`. A 2048 ubatch is faster and is what the gpt-oss guide uses on larger cards. It also grows the CUDA compute buffer, which is a common OOM on 8 GB.
- `--reasoning on`. `quality` passes `--reasoning-budget 10240` (llama.cpp b12110: `-1` unrestricted, `0` ends thinking immediately, `N>0` is a token cap) so the trace cannot eat the whole completion. `max` still passes `--reasoning-budget -1`. `day` passes `--reasoning-effort medium`. `max` passes `--reasoning-effort high` rather than the template's `xhigh` default, so a turn can finish inside the token budget. `quality` and `max` set chat-template kwargs to `{"preserve_thinking":true}`. That key is the Qwen template's switch for keeping prior thinking; gpt-oss does not get it. The launcher and the tuner put the JSON in `LLAMA_ARG_CHAT_TEMPLATE_KWARGS` (the name llama.cpp b12110 prints in `--help`) and leave `--chat-template-kwargs` off the command line. If a future llama.cpp build rejects the key, set `"preserve_thinking": false` in `runtime\overrides.json`.
- Sampling: Qwen profiles use temperature 0.6, top-p 0.95, top-k 20, repeat penalty 1.0. gpt-oss uses temperature 1.0 and top-p 1.0, which is the sampling the model was released with. Repeat penalty above 1.0 tends to damage thinking traces, so it stays at 1.0.

## Install from scratch on Windows 11

1. Install the current NVIDIA Game Ready or Studio driver for the RTX 4070 Laptop. Confirm it in PowerShell:

   ```powershell
   nvidia-smi
   ```

   You need a driver new enough for the CUDA 12.4 runtime that llama.cpp ships. You do not need the CUDA Toolkit. The `cudart` zip is the runtime.

2. Install Python 3.11 or newer, and Git:

   ```powershell
   winget install Python.Python.3.12
   winget install Git.Git
   ```

   Open a new PowerShell window so `py` and `git` are on `PATH`.

3. Clone this repository onto an SSD with room to spare. `quality` + `day` need about 35 GB for the weights, plus about 1 GB for llama.cpp, plus a few GB of headroom. Adding `max` needs another 19 GB. A page file of 16 GB or more on that same SSD is worth having: Windows can commit the mapped GGUF even when not every page is hot, and 32 GB of RAM is tight once the 22 GB quality model is resident.

4. Run setup, from the repo, on the Windows host:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\scripts\setup.ps1
   ```

   Setup creates `runtime\`:

   ```
   runtime\
     venv\            Python environment with this package
     llama.cpp\       llama-server.exe and the CUDA DLLs
     models\          GGUF files
     logs\
     overrides.json   written later by the tuner
     install.json     which llama.cpp build was unpacked
   ```

   `runtime\` is gitignored. Downloads go through `python -m local_agent download`, which retries and resumes a partial file. Run the same command again if the network drops.

5. Confirm the mock pipeline before you spend hours loading a model:

   ```powershell
   .\scripts\smoke-test.ps1
   ```

Other setup switches:

```powershell
.\scripts\setup.ps1 -Models day                 # only the 12 GB model
.\scripts\setup.ps1 -Models quality,day,max     # include the dense experiment
.\scripts\setup.ps1 -Cuda 13.4                  # only if the driver is new enough
.\scripts\setup.ps1 -InstallDir D:\ai-runtime   # weights on another drive
.\scripts\setup.ps1 -SkipGpuCheck               # layout check on a machine with no NVIDIA GPU
```

`-InstallDir` changes where weights and the venv live. Set `LOCAL_AGENT_RUNTIME` to that same path before `start-server.ps1` if it is not `runtime\` inside the repo. The scripts pass `-InstallDir` through, so the easier path is to pass the same directory every time.

## Pick a model

- Use `quality` when the task can run while you sleep and you care about the diff more than the clock. Thinking is on. Turns are long.
- Use `day` when you are at the machine. It is the configuration that finished the bench's agent tasks on 8 GB, because almost all of the weights are in RAM and VRAM stays near 2 GB.
- Use `max` when you want to see what a dense 27B does on this laptop and you already have a passing `quality` setup. Expect it to be the slow one.

Stop the server (Ctrl+C in its window) before switching profiles.

## Tune `--n-cpu-moe`

The shipped numbers are conservative. The tuner is how you spend the VRAM you actually have.

```powershell
# Stop llama-server first. The tuner binds the same port.
.\scripts\tune-n-cpu-moe.ps1 -Profile quality
.\scripts\tune-n-cpu-moe.ps1 -Profile day -HeadroomMib 1536
```

For each candidate in the profile (`quality`: 40, 36, 34, 32, 30; `day`: 30, 24, 20, 16; `max` uses `--n-cpu-ffn`: 64, 56, 48, 40) the tuner:

1. Starts `llama-server` at that offload and at the profile's real context size.
2. Waits until `/health` answers (default 300 seconds, which is long enough to mmap the quality model).
3. Generates a few tokens.
4. Reads `nvidia-smi` memory.used. Dedicated VRAM minus the headroom (default 1024 MiB) is the cap. Usage over the cap is a failure even if the process did not crash. On Windows a too-small `--n-cpu-moe` often "works" and then swaps the GPU over PCIe, which looks like a hang.
5. Stops at the first OOM or over-cap result, because the list is safest-first. Lowering the number further would only use more VRAM.
6. Writes the fastest success to `runtime\overrides.json`. If the server did not report tokens per second, the success that put the most weight on the GPU wins.

`start-server.ps1` reads that file. Delete a key, or the file, to go back to the shipped default. Unknown keys are rejected so a typo does not silently do nothing.

```json
{
  "quality": { "n_cpu_moe": 32 },
  "day": { "reasoning_effort": "high" }
}
```

Allowed keys include `n_cpu_moe`, `n_cpu_ffn`, `context`, `flash_attn`, `batch_size`, `ubatch_size`, `temperature`, `reasoning`, `reasoning_budget`, `reasoning_effort`, `preserve_thinking`, `reasoning_replay_turns`, `agent_max_tokens`, `agent_max_tokens_ceiling`, `port`, and `host`.

After the quality context moved from 40960 to 65536, run the tuner again. KV cache grows with context, and the offload that fit at 40K can sit on the cap at 64K. `.\scripts\tune-n-cpu-moe.ps1 -Profile quality -Check` loads the current offload once, does not write `overrides.json`, and fails if dedicated VRAM minus the headroom (default 1024 MiB) is exceeded. The full sweep is `.\scripts\tune-n-cpu-moe.ps1 -Profile quality`. During a successful check, `nvidia-smi` memory.used should stay about 1 GB under the 8 GB total.

## Run the server

```powershell
.\scripts\start-server.ps1 -Profile quality
```

Health check, from another window:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

The exact argument vector is saved under `runtime\logs\server-<profile>-<time>.argv.txt`. JSON settings are a `# env LLAMA_ARG_...=` line in that file, which is the value the process inherited.

`-Background` opens a new PowerShell window and returns. `overnight.ps1` does that when port 8080 is down.

The server log in that window is the place to look when a load fails. The harness log is separate: `runtime\logs\run-*\events.jsonl` has every request, the reasoning trace, every tool call, and every tool result.

## Overnight workflow

```powershell
.\scripts\overnight.ps1 -Task C:\src\my-app\TASK.md -Project C:\src\my-app -Profile quality
```

What it does:

1. Refuses to start unless `Project` is a git repo with a clean work tree. Pass `-InitRepo` when the folder is not a repository yet. That creates the folder if needed, runs `git init`, and makes an empty initial commit. The same flag on the Python CLI is `--init-repo`.
2. Suspends sleep for the life of the script (`SetThreadExecutionState`). The screen can still turn off.
3. Switches Windows to the High performance power scheme and restores the previous scheme at the end. This does not change Legion Vantage's thermal mode; set that yourself (below).
4. Starts `llama-server` if `/health` is down, and waits up to 20 minutes.
5. Creates `agent/<task-slug>-YYYYmmdd-HHMM`.
6. **Plan.** The model may read files. Its plan is saved as `AGENT_PLAN.md`.
7. **Steps, then implement.** After the plan, the model splits the work into 1–6 small steps (goal, files, one verify command). Each step is its own implement, check, and fix conversation with a fresh prompt: the task summary, a rebuilt repo map, the step list with finished steps marked, and the current step. A step that passes is snapshotted. A step that fails twice is replanned once; if that also fails, the run stops as `step_failed` and rolls back to the last good step. This is on by default (`steps: true`, 6 steps, 12 turns each, 1 replan) because a single 30-turn implement was hitting the turn budget and rewriting large files from memory. A reply that is not a step list falls back to one implement phase. Set `steps: false` on the task, in `AGENTS.md`, or on the profile to keep that single phase.
8. **Test.** The runner executes the task's `test_command` (default `python -m pytest -q`). On failure it starts a fresh conversation with the failure output and `git status`, and the model edits again. That repeat stops when tests pass, when `-MaxIterations` is hit (default 6), or when `-MaxHours` is hit (default 8). A fresh conversation per failure is deliberate: continuing one transcript is how the bench hit the context limit in the middle of a debug loop. After the tests and the smoke command pass, it runs each `verify_commands` entry (and, when extraction is turned on, backtick commands taken from Acceptance criteria). A failing verify command starts the same kind of fix round as a smoke failure.
9. **Review.** One call, no tools, with the full change set and the harness check results. New files are included. `git diff` by itself hides them until they are staged, which is what produced an empty review on a repo whose only commit was empty. The prompt includes the build or test command and whether it passed (with counts when the output has them), a short tail of that output, each `must_exist` item, the smoke result and a tail of its output, each verify command with its exit code and an output tail, the files changed since the task base, and the text of those files or a bounded excerpt. The model is told those checks already ran and are authoritative, so it must not ask to run or verify them again. It answers Correctness / Tests / Risks / Verdict and can tag a leftover item `[defect]` or `[verify-only]`. A reply that leaks `<tool_call>` markup or omits a required section is re-prompted twice and is not sent to review-fix. An `accept` or `fix` vote whose open items only ask to run, verify, check, or confirm something the harness already passed is accepted when `review_verify_only_pass` is on (the default). An acceptance line marked `not met` or `unverified` counts as one of those items when its only stated reason is that the harness did not run or execute the check. A real defect, including an ambiguous `not met` line, still rejects. A claim that a file or symbol is missing is dropped when that file or symbol is in the tree.
10. Writes `AGENT_REPORT.md` and commits it with the code. The committer name stays yours when `git config user.name` and `user.email` are set. If either is missing, the run still commits, prints a message that says so, and uses a one-shot `Local Coding Agent <local-agent@localhost>` for that commit only. Your git config is not changed.

The task file can set the commands in front matter:

```markdown
---
test_command: python -m pytest -q
lint_command: python -m ruff check .
verify_commands:
  - python app.py --frames 60
  - python app.py --frames 60 --ps1
---

# Short title

What should change, and how to tell it works.
```

`lint_command` runs only after the tests pass. `verify_commands` run after the tests and the smoke command pass. Each command gets `verify_timeout` seconds (default 120, maximum 600). The shell is bash on Linux and Windows PowerShell 5.1 on Windows, so a command you write here has to be something that shell can run. PowerShell 5.1 has no `&&`; a chain is split and each step is run on its own, stopping that chain at the first failure. A quoted program path needs the call operator (`& 'C:\path\app.exe' args`); the runner adds `&` when the command already starts with a quote. The sample task is `tasks\smoke-task.md`.

Optional front matter, all unset by default except the turn budgets and the exploration cap:

| Key | Default | Meaning |
| --- | --- | --- |
| `must_exist` | unset | Paths that must exist, or names that must show up in the test output, or the test gate fails. |
| `plan_turns` | 8 | Plan-phase turn budget. Also a profile / `overrides.json` key. |
| `implement_turns` | 30 | Implement, fix, and review-fix turn budget. Same precedence. |
| `exploration_cap` | 10 | Read-only calls in a row before a "write the files now" nudge. `0` turns it off. |
| `exploration_bonus_turns` | 0 | One extra budget when the phase ran out of turns while exploring. |
| `cheat_sheet_marker` | `cheat sheet` | Heading substring copied verbatim into the implement, fix, and review-fix system prompt. |
| `api_error_patterns` | `CS1503`, `CS0214`, `CS1061`, `CS0117` | Substrings that re-inject the cheat sheet after a compile error. |
| `blocked_probes` | ilspycmd, `dotnet new`, reflection loads, and similar | Extra shell patterns that are refused. |
| `scratch_dirs` | `diag`, `apicheck`, `api-check`, `_probe`, `scratch`, `throwaway` | Directories that cannot be created unless the plan names them. |
| `verify_commands` | unset | Commands run after tests and smoke pass. Each exit code and an output tail are shown to the reviewer. A nonzero exit starts a fix round, the same way a smoke failure does. |
| `verify_timeout` | 120 | Seconds for each verify command. Clamped to 1–600. |
| `extract_verify_commands` | `false` | When `true`, also run backtick-quoted commands found in Acceptance criteria. Off unless the task, `AGENTS.md`, or the profile sets it, because a guessed command is easy to get wrong. Flag fragments such as `` `--frames 60` `` are not extracted. |
| `review_verify_only_pass` | `true` | Accept a review that votes `accept` or `fix` when every open item only asks to run or verify a check the harness already passed. A `not met` or `unverified` line whose only reason is that the harness did not run the check counts as one of those items. A real defect still starts review-fix. Set `false` on the task, in `AGENTS.md`, or on the profile to keep that vote as review-fix. |
| `repo_map` | `true` | Build a symbol index of the target repo and put it in the plan and implement context. Set `false` to turn it off. |
| `repo_map_tokens` | `4000` | Token budget for that index (about 4 characters per token). Clamped to 200–6000. |
| `steps` | `true` | After the plan, split the work into small checked steps. On by default so a multi-file task is not one 30-turn implement. Set `false` for the single implement phase. A breakdown that is not a step list falls back to that phase either way. |
| `max_steps` | `6` | Cap on the step list. One step is allowed. Clamped to 1–8. |
| `step_turns` | `12` | Turn budget for each step's implement phase and for each of its fix rounds. The outer fix and review-fix still use `implement_turns`. |
| `step_replans` | `1` | How many times a step that failed twice may replan the remaining work. `0` stops at the second failure. Clamped to 0–3. |
| `api_lookup` | `true` | Index referenced NuGet packages and expose `lookup_api`. On a CS1061, CS1503, CS0117, CS7036, or similar error, the fix prompt gets the real signatures. Set `false` to turn that off. |
| `api_lookup_budget` | `1600` | Characters kept from one lookup. Clamped to 200–4000. |

The same numeric keys can sit on the profile in `profiles\models.json` or under that profile in `runtime\overrides.json`. A value in the task file wins over the profile. Passing an explicit turn budget to the Python function (the tests and the eval runner do this) wins over the task file. See [docs/TASK_TEMPLATE.md](docs/TASK_TEMPLATE.md).

Shell commands the model issues are killed at 120 seconds unless it passes a lower `timeout_seconds`. The hard cap is 600 seconds. `git push`, `git reset --hard`, `git clean`, `shutdown`, `diskpart`, and `rm -rf /` are refused. That list is a tripwire for an unattended run, not a sandbox: the model can run anything you can run.

Logs:

- `runtime\logs\overnight-*.log` is the PowerShell transcript.
- `runtime\logs\run-*\events.jsonl` is every model turn. `events.log` is the same stream as one line per event. Step runs log `step_start`, `step_pass`, `step_fail`, and `replan` with the step index.

## Project context and the task template

The target repo should have an `AGENTS.md` at its root. That file, plus any paths named in a task's `context:` front matter, is inlined as a **Project context** block at the start of every phase (plan, implement, fix, review). The block is capped at 14,000 characters. When a file is cut, the block keeps sections that match the task or a cheat-sheet heading, says which headings were omitted, and still says `[truncated - read <path> for the rest]`. Every path stays in the list so the model can read the remainder. A truncation is also written to `events.log` as `project_context_truncated`.

A **repo map** is appended after that block, in its own budget, so it does not steal characters from `AGENTS.md`. It lists files and their symbols (C# namespaces, classes, methods, and fields, plus Python, with a short fallback for other source files). `bin/`, `obj/`, `.git`, build output, vendored directories, and `.gitignore` matches are skipped. Files mentioned by the task, named in the plan, changed recently, or referenced by those files are ranked first. The default budget is 4,000 tokens (about 16,000 characters). Set `repo_map: false` or `repo_map_tokens` on the task, in `AGENTS.md`, or on the profile (`runtime\overrides.json`). The task wins, then `AGENTS.md`, then the profile. The map is rebuilt before each later phase so new symbols show up, and it stays in the first user message only, which compaction keeps. Its size is logged as `repo map N chars`.

`list_files` lists a directory with the same skips. A recursive `Get-ChildItem -Recurse`, `ls -R`, or `find` is answered the same way instead of walking `bin/` and `obj/`.

`lookup_api` reads the public API of NuGet packages referenced by `obj/project.assets.json` (or by `PackageReference` when that file is missing). It uses each package's XML docs in the NuGet cache, and the assembly metadata when a package ships no XML file. Results are cached per package version under `runtime/api-index`. A fix or step-fix prompt that contains `CS1061`, `CS1503`, `CS0117`, `CS7036`, or a similar member error gets those signatures injected. The task wins over `AGENTS.md`, which wins over the profile. Lookups are logged as `api_lookup` (the tool) and `api_autolookup` (compiler errors).

Compaction keeps the first user message, so the project context and the one repo map are not dropped when tool output is trimmed. The prompt itself is capped at 32,768 estimated tokens even though the quality context is 65,536, so the completion still has room.

Write tasks with [docs/TASK_TEMPLATE.md](docs/TASK_TEMPLATE.md). Required headings are `Goal`, `Acceptance criteria`, and `Out of scope`. A non-Python repo must set `test_command`. A Python repo that omits it uses `python -m pytest -q`.

```powershell
python -m local_agent validate C:\src\my-app\tasks --project C:\src\my-app
```

The queue checks every task file before the first one starts, including markdown in task-list subfolders, and refuses with the full list of problems. `-NoStrict` (`--no-strict`) runs older files, including `tasks/ui/*`, that predate the template. Files under `tasks\archive` are not checked and are not run.

The self-review must answer `Acceptance criteria` (each line `met` or `not met`), `Out-of-scope files touched`, and `Conflicts with AGENTS.md or decisions`. A `not met` line, a conflict, or a concrete defect runs one fix iteration and a second review. A `not met` or `unverified` line whose only stated reason is that the harness did not run or execute the check is not that kind of defect: with `review_verify_only_pass` on and the harness checks green, an `accept` or `fix` vote that contains only those items passes and logs `review_verify_only_pass`. An ambiguous line still rejects. The review-fix prompt tells the model to change only those findings and not to rewrite files that already work. If that fix turns a passing test run into a failing one, the runner restores the last passing snapshot and records `review_fix_reverted`. A review that is tool-call markup, or that is still missing a required section after two re-prompts, is `review_malformed`. It is not forwarded to review-fix. The diff in the review prompt starts with the file list. If the patch does not fit, the prompt names the files that were left out. The same prompt includes the harness check results and the changed files, and tells the model those results are authoritative. When `review_verify_only_pass` is true (the default) and build, tests, `must_exist`, smoke, and verify commands all passed, an `accept` or `fix` vote is accepted if every open item only asks to run, verify, check, or confirm. The runner writes a `review_verify_only_pass` event listing those items. A finding that a file or symbol is missing is dropped, and logged as `review_finding_contradicted`, when the tree contains it. A real defect still goes to review-fix. Set `review_verify_only_pass: false` on the task, in the target repo's `AGENTS.md`, or on the profile to turn that acceptance off. The task wins over `AGENTS.md`, which wins over the profile. `verify_commands` and `extract_verify_commands` follow that same order. Extraction stays off until one of them sets it.

## Guards from failed runs

These are on for every task. They do not change a task file that already passes.

- **Exploration cap.** After 10 read-only or probing calls in a row with no file write (`exploration_cap`), the harness adds `Stop exploring. Write the files now and build early.` If the model keeps going, further reads are refused and the phase can stop as `exploration_capped`. Shell commands that decompile or reflect (`ilspycmd`, `ildasm`, `javap`, `Assembly.Load`, and the other built-in patterns), scaffold a new project (`dotnet new`, `npm init`, `cargo new`, `go mod init`), or create a scratch directory outside the plan (`diag`, `apicheck`, …) are refused immediately. A new `.csproj`, `.fsproj`, `.vbproj`, or `.sln` is refused unless the plan names that path. Editing a project file that already exists is allowed.
- **Cheat sheet.** A heading in `AGENTS.md` or a `context:` file whose text contains `cheat sheet` (or `cheat_sheet_marker`) is copied verbatim into the system prompt for implement, fix, and review-fix. It is not taken from the truncated Project context block. When test output contains `CS1503`, `CS0214`, `CS1061`, or `CS0117` (or `api_error_patterns`), the fix prompt repeats the sheet.
- **False pass.** A passing test command is not success while the cumulative diff from the task's base commit is empty, or does not substantively touch a file the plan said it would add or edit. That diff includes later commits on the agent branch, so a `git commit` during the run does not make the work look empty. Implement stopping on the turn budget or the exploration cap does not keep the guard closed once the diff is real, and a fix round that changes files clears it on the next passing gate. The runner writes a `false_pass_guard` event when it blocks or releases. If the guard is still blocking after two fix rounds in a row that change no files while the gate passes, it releases, logs why, and goes to review. `must_exist` is checked in the same test gate. A one-line edit of a file the plan named is enough; that is not a near-empty diff.
- **Snapshots.** Every passing test iteration is stored as a commit on `refs/asimov/snapshots/<branch-with-slashes-replaced>/<n>` without moving HEAD, and the ref is written to `events.jsonl`. The refs are kept. If the run ends with failing checks and a snapshot exists, that snapshot is restored before the final commit. `git show <ref>` still works afterwards.
- **Plain text.** Plan, review, and the second review strip leaked `<tool_call>` markup. Review is re-prompted up to twice and must be written from the diff and the harness checks already in the prompt.
- **Verify-only review.** The reviewer has no tools. If it votes `accept` or `fix` and every open item only asks for a run or a check the harness already passed, and `review_verify_only_pass` is true, the run continues as a pass and logs `review_verify_only_pass`. An acceptance line marked `not met` or `unverified` is one of those items when its only reason is that the harness did not run or execute the check. A failing check, a `not met` line that describes a defect, an ambiguous line, or any other defect still starts review-fix. A missing-file claim that the tree contradicts is dropped and logged as `review_finding_contradicted`.
- **Verify commands.** After tests and smoke pass, the runner executes `verify_commands` and records each exit code and an output tail in the review prompt. A failure starts a fix round. Backtick commands in Acceptance criteria are extracted only when `extract_verify_commands` is true.
- **BOM.** `load_task` reads task files as UTF-8 with the BOM stripped. `validate` says when front matter cannot be parsed.

`exploration_bonus_turns` defaults to 0, so a phase that spends its budget exploring does not silently get more turns. Set it on the task or the profile when you want one bonus budget for that case.

`AGENT_PLAN.md` and `AGENT_REPORT.md` are written to `<project>\.agent\`. The runner adds `.agent/` to `.git\info\exclude`, which is local to the clone and is not `.gitignore`, so `git add -A` does not commit them. The report includes an `Open questions` section. The result JSON still has the report path.

Run a queue on an integration branch, not on `main`. Starting on `main` or `master` stops unless you pass `-AllowMain` (`--allow-main`), which also prints a warning.

## Task queue

Use this when a job is split into ordered markdown files and nobody will be around to merge and start the next one.

```text
C:\src\my-app\tasks\
  01-runner.md
  02-status.md
  03-window.md
  04-notify.md
```

```powershell
.\scripts\queue.ps1 -TaskDir C:\src\my-app\tasks -Project C:\src\my-app -Profile quality
```

The same thing without the Windows stay-awake, power scheme, server launch, or toast:

```powershell
python -m local_agent queue --task-dir C:\src\my-app\tasks --project C:\src\my-app --profile quality
```

What it does:

1. Holds sleep off and switches to the High performance power scheme for the whole queue, then restores the previous scheme. `llama-server` is started once if it is not already healthy.
2. Runs task markdown in the folder and in task-list subfolders, in natural path order (`2` before `10`, then `ui\step-2.md`). `README.md`, `queue-summary.md`, and anything under `archive\`, `done\`, or `failed\` are not tasks. A custom `-ArchiveDir` is skipped too, including when that folder sits inside the queue.
3. Each file is an overnight run. The branch checked out at the start is remembered in `queue-state.json`.
4. When tests and the review pass, that agent branch is merged with `--no-ff` back into the remembered branch. The next file starts from the merge.
5. When a task fails, the queue stops. The failed branch is not merged, and the project is checked out on the base branch again so a later retry does not build on the failure. The task file stays where it is, so you can edit it and run the same command again. `-ContinueOnFailure` runs the remaining files from that last good merge anyway.
6. A passing file is moved into `tasks\archive` in this repo. The path relative to the tasks root is kept: `tasks\foo.md` becomes `tasks\archive\foo.md`, and `tasks\ui\step-2.md` becomes `tasks\archive\ui\step-2.md`. The mirror folder is created if it is missing. If that name is already in the archive, the new file gets a `-YYYYmmdd-HHMMSS` suffix and the old file is left alone. `-ArchiveDir D:\task-archive` changes the root. `-TasksRoot` changes the folder the relative path is measured from. `-NoArchive` keeps the older layout: passing files move to `done\` and failing files to `failed\`.
7. `queue-summary.md` in the task folder lists every task, then which files were archived and which stayed. When the queue ends, Windows shows a toast and plays a beep. If the toast cannot be shown, the script warns and still exits with the real result.

`-InitRepo` and `-MaxHoursPerTask` match the overnight switches. `-MaxHoursPerTask` is the budget for each file, not the whole queue.

Stop the queue after the task that is running, so you can shut the server down without waiting for the rest of the folder. From another PowerShell window:

```powershell
.\scripts\stop-queue.ps1
python -m local_agent stop
```

The running task finishes. Nothing after it starts. The summary says `Stopped on request` and lists the files still pending. The stop file is deleted and the process exits 0.

Withdraw the request if the queue should keep going:

```powershell
.\scripts\stop-queue.ps1 -Cancel
python -m local_agent stop --cancel
```

The request file is `runtime\STOP_QUEUE` (or `LOCAL_AGENT_RUNTIME\STOP_QUEUE`). A file that is already there when a queue starts is stale: the queue warns, deletes it, and runs. Create the request after the queue has started. Ctrl+C in the queue window is not this. Windows hands that interrupt to `python.exe`, which aborts the task in progress. That is not reliable as a two-stage stop on Windows PowerShell 5.1 and PowerShell 7, so the scripts do not treat a second Ctrl+C as special.

A single overnight run archives the task file on success the same way. A failed run leaves the file in place.

```powershell
.\scripts\overnight.ps1 -Task .\tasks\ui\step-2.md -Project C:\src\my-app -Profile quality
.\scripts\overnight.ps1 -Task .\tasks\ui\step-2.md -Project C:\src\my-app -NoArchive
python -m local_agent archive .\tasks\ui\step-2.md
python -m local_agent archive .\tasks\foo.md --archive-dir D:\task-archive --tasks-root .\tasks
.\scripts\queue.ps1 -TaskDir .\tasks -Project C:\src\my-app -Profile quality -ArchiveDir D:\task-archive
.\scripts\queue.ps1 -TaskDir .\tasks\ui -Project C:\src\my-app -Profile quality -NoArchive
```

Point `-TaskDir` at one list (`tasks\ui`) when you do not want every list under `tasks` to run in the same queue.

If the time budget hits first, the branch and the report are still written. The script's exit code is 0 only when the test command passed.

## Smoke test

`tasks\smoke-task.md` asks for `adder.py` and `test_adder.py`.

- `.\scripts\smoke-test.ps1` runs that pipeline with a scripted model. It writes a deliberate bug (`return a - b`), pytest fails, the next phase edits it to `return a + b`, pytest passes, and git commits. No weights and no GPU.
- `.\scripts\smoke-test.ps1 -Live -Profile day` sends the same task to a server you already started, in a throwaway repo under the temp directory. Budget defaults to 45 minutes.

On a machine that is not Windows, the same mock is:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m local_agent smoke --mock
```

## Expected speed and memory

These are not measurements from a Legion 5i. They are the published figures this configuration was chosen from, plus what the file sizes imply on 32 GB.

**quality.** The witcheer bench (RTX 4060 Ti 8 GB, desktop, May 2026) reported about 35 tok/s and 7.2 GB at `--n-cpu-moe 30`, and about 18 minutes for an easy agent task with thinking off. Thinking on stretched that same task to about 49 minutes. A hard task ran 18 to 87 minutes and was still going when the context or the operator gave up. A 4070 Laptop at the shipped `--n-cpu-moe 34` should be in that same decode band, not in the hundreds of tok/s: the experts are read from DDR5, not from GDDR6. Plan on the 22 GB file being resident, so close browsers and games. Leave the page file enabled. Do not add `--mlock`; pinning the whole file will fail on 32 GB.

**day.** The same bench recorded about 1.8 GB of VRAM at `--n-cpu-moe 30` and finished 10/10 agent tasks. Decode is again RAM-bandwidth bound. 12 GB of weights fits next to Windows without much drama. Wall-clock for a multi-file task is still minutes to tens of minutes, because an agent loop is dozens of round trips and each trip pays the CPU expert pass.

**max.** Every token reads a dense 27B feed-forward from RAM. Treat it as an overnight experiment. Context is 16K so the KV cache of its 16 full-attention layers does not crowd the card.

Prefill on a long agent prompt is slower than the decode number. The generation budget on `quality` (up to 24576 tokens) exists so the model can think and still emit a tool call. At a few tens of tokens per second, one turn can be many minutes. The HTTP client waits up to an hour per turn on `quality` and `max`, and 30 minutes on `day`.

## Legion, power, and heat

- Plug in the charger. On battery, Lenovo and Windows cut CPU and GPU power, and an overnight run will not finish.
- In Lenovo Vantage or Legion Space, set the thermal mode to Performance (or a custom mode with the higher power limits) before a long run. The script can switch the Windows power scheme to High performance. It cannot press the Legion performance button for you.
- Give the laptop air. The rear intake should not sit on bedding. A lap or a closed cabinet will thermal-throttle a 35B run into something much slower than the numbers above.
- NVIDIA Control Panel, Manage 3D settings, Program Settings for `llama-server.exe`: Power management mode = Prefer maximum performance. Otherwise the 4070 clocks down between tokens.
- Hybrid graphics (the Intel iGPU drives the internal display) leaves more of the 8 GB free for the model. Discrete / MUX mode gives the display to the 4070 and spends a few hundred MB on the desktop. Hybrid is the better default here.
- Pause Windows Update or set active hours before an overnight run. The stay-awake call does not block a forced reboot.
- One model, one server. Do not also run a game, a local diffusion UI, or a second llama-server.

## Troubleshooting

**`nvidia-smi` is not recognized.** The driver is missing or the shell was opened before the install. Install the NVIDIA driver and open a new window. The CUDA Toolkit is not required.

**`cudart64_12.dll` (or similar) was not found.** Only the `llama-bNNNN-bin-win-cuda-*.zip` was extracted. Setup extracts that zip and `cudart-llama-bin-win-cuda-*.zip` into the same folder. Run `.\scripts\setup.ps1` again, or unpack the cudart zip next to `llama-server.exe`.

**CUDA error on startup, or the process exits during load.** The usual causes, in order:

- The driver is older than the CUDA runtime. Stay on `-Cuda 12.4` unless you know the driver supports 13.x.
- Flash attention disagrees with this driver build. Add `"flash_attn": "off"` under the profile in `runtime\overrides.json` and start again.
- The GPU is out of memory. Increase `n_cpu_moe` (try 40 for `quality`, which is all expert layers) or lower `context` to 32768. Then rerun the tuner.
- Something else is on the GPU. `nvidia-smi` shows the other process. Close it.

**The load "succeeds" and then generation is extremely slow.** Windows allowed the allocation to spill past the 8 GB. In Task Manager, look at Dedicated GPU memory, not Shared. If dedicated is pegged at 8 GB, the offload is too aggressive. Raise `n_cpu_moe` or rerun `tune-n-cpu-moe.ps1` with a larger `-HeadroomMib`.

**The machine runs out of RAM (commit charge, page file thrashing, process killed).** Close browsers. Confirm the page file is enabled and at least 16 GB. Do not pass `--mlock`. The quality model wants about 22 GB resident. If Task Manager shows the commit charge near the limit before the model finishes loading, the page file is too small or another program is holding RAM.

**Empty replies, or the agent says the model returned nothing.** This is the thinking-model failure mode. The trace consumed `max_tokens` and `content` is empty. The harness already retries at `agent_max_tokens_ceiling` and then sends one short nudge. If it still happens:

- Confirm the server was started by `start-server.ps1`. A hand-built command with `-n 512` or `--reasoning-budget 0` will empty every turn. The shipped quality budget is 10240, which leaves room under `max_tokens` for the tool call. Raise it in `runtime\overrides.json` only if turns are cut off mid-thought, and keep it under `agent_max_tokens`.
- In `runtime\overrides.json`, raise `agent_max_tokens` and `agent_max_tokens_ceiling`. Keep their sum under `context` minus a few thousand tokens of prompt.
- For a faster daytime loop where you do not need the trace, set `"reasoning": "off"` on `quality`. The bench found thinking off about 2.7x faster on the easy task, and the tool calls still worked. Overnight quality runs should leave reasoning on.

**`context size` / `n_ctx` errors in the server window.** The prompt did not fit. The harness compacts and retries once. If it keeps happening, the task's test output is huge: the runner already truncates what it pastes back, but a profile `context` of 16384 (`max`) will feel cramped. Raise `context` only if `nvidia-smi` shows the headroom, then retune.

**`error while handling argument "--chat-template-kwargs"`, `json.exception.parse_error.101`, last read `'{p'`.** PowerShell 5.1 dropped the double quotes inside the JSON, so llama-server read `{preserve_thinking:true}`. Start with `.\scripts\start-server.ps1`. That script and `tune-n-cpu-moe.ps1` (including `-Check`) put `{"preserve_thinking":true}` in `LLAMA_ARG_CHAT_TEMPLATE_KWARGS` and leave the flag off the command line. Live smoke and live eval talk to a server already started that way. A window where you start `llama-server.exe` yourself needs the same variable set before the process starts. `runtime\logs\server-<profile>-<time>.argv.txt` lists the real arguments and a `# env` line for the JSON. The tuner writes the same `# env` line at the top of `runtime\logs\tune\<profile>\*.log`.

**Port 8080 is already in use.** Another `llama-server` is running. Close that window, or start with `-Port 8081` and pass the same port to `overnight.ps1`.

**Download stopped at a few GB.** Run the same `setup.ps1` or `download-models.ps1` command again. The partial file is resumed. A size mismatch at the end means the file is still short; resume rather than deleting it.

**`is not a git repository`.** The project folder has no `.git` directory. Re-run with `-InitRepo` (`--init-repo` on the Python CLI) to create the repository and an empty initial commit, or run `git init` yourself. A folder that already has files is still refused after that until those files are committed or moved aside, so the agent does not mix its branch with existing work.

**`git commit` failed or the report says the repo was dirty.** Commit or stash your own work first. The runner will not mix its branch with uncommitted files. If `user.name` or `user.email` is unset, the transcript and `AGENT_REPORT.md` say that the commit used `Local Coding Agent <local-agent@localhost>` and did not change git config. Set the identity with `git config --global user.name` and `git config --global user.email` when you want the commits under your name.

**Tests still fail right after a successful edit, and the traceback shows the old code.** Python records `.pyc` timestamps in whole seconds. A test run and an edit in the same second can leave pytest executing the previous bytecode. The edit and write tools delete that file's `__pycache__` entry, and the runner clears project bytecode before each test command. If you run pytest yourself in the same second, delete `__pycache__` or set `PYTHONDONTWRITEBYTECODE=1`.

**The model repeats the same edit.** The phase stops after the warning. Read `events.jsonl` for the tool error it ignored, fix the task text so the goal is narrower, and run again. Raising the iteration budget does not help a model that is not reading the tool error.

**Live smoke fails and the mock smoke passed.** The harness is fine and the model or the server is the variable. Check `/health`, the server window for OOM, and `events.jsonl` for `empty_reply` or `loop_detected`. Try `day` before `quality` if you want a faster signal.

## Add a model later

Add an object under `profiles` in [`profiles/models.json`](profiles/models.json). Copy `day` for a MoE model or `max` for a dense one. Set:

- `repo`, `filename`, `download_url`, `size_bytes` (the Hugging Face `x-linked-size` header, or the file size after a finished download)
- `moe`, `block_count`
- `context`, `n_cpu_moe` or `n_cpu_ffn`, `tune_candidates` (safest / highest offload first)
- `reasoning`, `reasoning_effort` or `reasoning_budget`
- `agent_max_tokens` and a larger `agent_max_tokens_ceiling`
- `optional: true` if it should not download with the default setup

Then:

```powershell
.\scripts\download-models.ps1 -Models my-new-profile
.\scripts\tune-n-cpu-moe.ps1 -Profile my-new-profile
.\scripts\start-server.ps1 -Profile my-new-profile
```

Check the new file against a current llama.cpp. Architecture support moves. A model llama-server cannot load will say so in the server window during the first start; the tuner will record that candidate as a crash and stop.

Keep the tool list as it is. Adding tools, or nesting the schemas, is the change that made smaller local models fail the bench.

## Develop and test

From any machine with Python 3.11+ and git, no GPU required:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m local_agent smoke --mock
python -m local_agent eval --mock
python -m local_agent eval --mock --only off-by-one
```

`eval --mock` does not load a model. It checks that each fixture under `evals/` fails its hidden checker on the starter tree and passes on the reference solution. `python -m local_agent eval --profile quality` runs those tasks against a server you already started and writes `runtime/evals/scoreboard-*.json`. `--only name` runs one fixture. Live evals are not part of CI.

GitHub Actions runs the unit tests, the mock smoke, and `eval --mock` on Ubuntu. The PowerShell scripts are checked for balanced braces and for `ErrorActionPreference = 'Stop'`. They are not executed in CI. Run `.\scripts\smoke-test.ps1` once on the laptop after setup.

Useful module entry points, which the scripts call:

```text
python -m local_agent profiles
python -m local_agent releases --cuda 12.4
python -m local_agent server-args --profile quality --allow-missing-model
python -m local_agent download --url <url> --dest <path> --expected-size <bytes>
python -m local_agent health --profile quality
python -m local_agent tune --profile quality
python -m local_agent overnight --task tasks/smoke-task.md --project <git-repo> --profile quality
python -m local_agent archive tasks/ui/step-2.md
python -m local_agent stop
python -m local_agent stop --cancel
python -m local_agent eval --mock
python -m local_agent eval --profile quality --only tkinter-launch
```

## What was not run on an RTX 4070

This project was written and tested without an 8 GB NVIDIA GPU. The unit tests cover profile flags, release-zip selection, resumable downloads, the tool jail, shell timeouts, tool-call parsing, context compaction, loop detection, empty-reply retry, and a full overnight loop against a fake server. They do not load a GGUF, and they do not measure tokens per second.

Before trusting a long unattended job on the Legion:

1. `nvidia-smi` shows the 4070 and a current driver.
2. `.\scripts\setup.ps1` finishes, including both CUDA zips.
3. `.\scripts\smoke-test.ps1` passes (mock).
4. `.\scripts\tune-n-cpu-moe.ps1 -Profile quality` finishes and `nvidia-smi` during a successful candidate stays about 1 GB under the dedicated total. After this repo's context change, run that tune again, or `.\scripts\tune-n-cpu-moe.ps1 -Profile quality -Check` for a single load of the current offload.
5. `.\scripts\smoke-test.ps1 -Live -Profile quality` passes.
6. `python -m local_agent eval --mock` passes, then `python -m local_agent eval --profile quality` if you want the fixture scoreboard.
7. Then point `overnight.ps1` or `queue.ps1` at a real repository. The queue retries a failed task once. Pass `-Retries 0` to disable that.
