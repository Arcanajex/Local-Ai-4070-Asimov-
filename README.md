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
```

Plug the laptop in before `setup.ps1` downloads anything large, and leave it plugged in for every server and overnight run.

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
- Thinking text is logged and is not sent back on the next turn. Replaying it is what filled the 32K window in the bench.
- If a turn comes back empty because `finish_reason` is `length`, the harness retries once at the profile's higher token ceiling, then nudges the model once. Thinking models spend the budget inside the trace and return no `content` when `max_tokens` is small.
- The overnight runner, not the model, executes the test command, writes `AGENT_REPORT.md`, and commits. The model is told not to `git commit` or `git push`.

The harness uses the Python standard library only. Tests mock the HTTP API and also run one scripted overnight loop that really executes pytest and git. See [What was not run on an RTX 4070](#what-was-not-run-on-an-rtx-4070).

## Models

Checked on 2026-11-10 against Hugging Face (`x-linked-size`) and the llama.cpp server README at binary tag `b12110` (the build attached to release `v0.9.0`). `scripts\setup.ps1` resolves the latest Windows CUDA release itself; it does not pin `b12110`.

| Profile | When to use | Weights | File size | Context | Offload | Generation budget |
| --- | --- | --- | --- | --- | --- | --- |
| `quality` | Overnight. Accuracy over speed. | [`unsloth/Qwen3.6-35B-A3B-GGUF`](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF) `Qwen3.6-35B-A3B-UD-Q4_K_M.gguf` | 22,134,528,992 bytes (22.1 GB) | 40960 | `--n-cpu-moe 34` | 16384, retry 24576 |
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

These are built by `python -m local_agent server-args` and passed through by `start-server.ps1`.

- `--host 127.0.0.1` so the API is not on the network. There is no API key because nothing outside the machine should be able to reach it.
- `--n-gpu-layers all` so attention (and, for MoE, the non-expert weights) can sit on the GPU. Expert or FFN placement is the other flag.
- `--parallel 1` so the KV cache is not allocated more than once.
- `--fit off`. llama.cpp 0.9 will otherwise change unset values to fit VRAM, including shrinking the context. The context above is intentional. Fit is how a 40K agent loop silently becomes a 4K one.
- `--jinja`. Tool calling uses the chat template stored in the GGUF. Current llama-server builds default this on; the flag is still passed.
- `--flash-attn on` and `--cache-type-k q8_0 --cache-type-v q8_0`. Quantized KV needs flash attention on CUDA.
- `--batch-size 512 --ubatch-size 512`. A 2048 ubatch is faster and is what the gpt-oss guide uses on larger cards. It also grows the CUDA compute buffer, which is a common OOM on 8 GB.
- `--reasoning on`. `quality` and `max` also pass `--reasoning-budget -1` (no cap inside the template). `day` passes `--reasoning-effort medium`. `max` passes `--reasoning-effort high` rather than the template's `xhigh` default, so a turn can finish inside the token budget.
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

Allowed keys include `n_cpu_moe`, `n_cpu_ffn`, `context`, `flash_attn`, `batch_size`, `ubatch_size`, `temperature`, `reasoning`, `reasoning_budget`, `reasoning_effort`, `agent_max_tokens`, `agent_max_tokens_ceiling`, `port`, and `host`.

## Run the server

```powershell
.\scripts\start-server.ps1 -Profile quality
```

Health check, from another window:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

The exact argument vector is saved under `runtime\logs\server-<profile>-<time>.argv.txt`.

`-Background` opens a new PowerShell window and returns. `overnight.ps1` does that when port 8080 is down.

The server log in that window is the place to look when a load fails. The harness log is separate: `runtime\logs\run-*\events.jsonl` has every request, the reasoning trace, every tool call, and every tool result.

## Overnight workflow

```powershell
.\scripts\overnight.ps1 -Task C:\src\my-app\TASK.md -Project C:\src\my-app -Profile quality
```

What it does:

1. Refuses to start unless `Project` is a git repo with a clean work tree.
2. Suspends sleep for the life of the script (`SetThreadExecutionState`). The screen can still turn off.
3. Switches Windows to the High performance power scheme and restores the previous scheme at the end. This does not change Legion Vantage's thermal mode; set that yourself (below).
4. Starts `llama-server` if `/health` is down, and waits up to 20 minutes.
5. Creates `agent/<task-slug>-YYYYmmdd-HHMM`.
6. **Plan.** The model may read files. Its plan is saved as `AGENT_PLAN.md`.
7. **Implement.** Read, write, edit, and shell, up to 30 turns.
8. **Test.** The runner executes the task's `test_command` (default `python -m pytest -q`). On failure it starts a fresh conversation with the failure output and `git status`, and the model edits again. That repeat stops when tests pass, when `-MaxIterations` is hit (default 6), or when `-MaxHours` is hit (default 8). A fresh conversation per failure is deliberate: continuing one transcript is how the bench hit the context limit in the middle of a debug loop.
9. **Review.** One call, no tools, with the diff. The model answers Correctness / Tests / Risks / Verdict.
10. Writes `AGENT_REPORT.md` and commits it with the code. The committer name stays yours when `git config user.name` is set. A repo with no identity gets a one-shot `Local Coding Agent <local-agent@localhost>` for that commit only; your global git config is not changed.

The task file can set the commands in front matter:

```markdown
---
test_command: python -m pytest -q
lint_command: python -m ruff check .
---

# Short title

What should change, and how to tell it works.
```

`lint_command` runs only after the tests pass. The shell is PowerShell, so a command you write here has to be something PowerShell can run. The sample task is `tasks\smoke-task.md`.

Shell commands the model issues are killed at 120 seconds unless it passes a lower `timeout_seconds`. The hard cap is 600 seconds. `git push`, `git reset --hard`, `git clean`, `shutdown`, `diskpart`, and `rm -rf /` are refused. That list is a tripwire for an unattended run, not a sandbox: the model can run anything you can run.

Logs:

- `runtime\logs\overnight-*.log` is the PowerShell transcript.
- `runtime\logs\run-*\events.jsonl` is every model turn.

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

- Confirm the server was started by `start-server.ps1`. A hand-built command with `-n 512` or `--reasoning-budget 0` will empty every turn.
- In `runtime\overrides.json`, raise `agent_max_tokens` and `agent_max_tokens_ceiling`. Keep their sum under `context` minus a few thousand tokens of prompt.
- For a faster daytime loop where you do not need the trace, set `"reasoning": "off"` on `quality`. The bench found thinking off about 2.7x faster on the easy task, and the tool calls still worked. Overnight quality runs should leave reasoning on.

**`context size` / `n_ctx` errors in the server window.** The prompt did not fit. The harness compacts and retries once. If it keeps happening, the task's test output is huge: the runner already truncates what it pastes back, but a profile `context` of 16384 (`max`) will feel cramped. Raise `context` only if `nvidia-smi` shows the headroom, then retune.

**Port 8080 is already in use.** Another `llama-server` is running. Close that window, or start with `-Port 8081` and pass the same port to `overnight.ps1`.

**Download stopped at a few GB.** Run the same `setup.ps1` or `download-models.ps1` command again. The partial file is resumed. A size mismatch at the end means the file is still short; resume rather than deleting it.

**`git commit` failed or the report says the repo was dirty.** Commit or stash your own work first. The runner will not mix its branch with uncommitted files. If `user.email` is unset, the commit still happens with a local one-shot identity.

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
```

GitHub Actions runs that on Ubuntu. The PowerShell scripts are checked for balanced braces and for `ErrorActionPreference = 'Stop'`. They are not executed in CI. Run `.\scripts\smoke-test.ps1` once on the laptop after setup.

Useful module entry points, which the scripts call:

```text
python -m local_agent profiles
python -m local_agent releases --cuda 12.4
python -m local_agent server-args --profile quality --allow-missing-model
python -m local_agent download --url <url> --dest <path> --expected-size <bytes>
python -m local_agent health --profile quality
python -m local_agent tune --profile quality
python -m local_agent overnight --task tasks/smoke-task.md --project <git-repo> --profile quality
```

## What was not run on an RTX 4070

This project was written and tested without an 8 GB NVIDIA GPU. The unit tests cover profile flags, release-zip selection, resumable downloads, the tool jail, shell timeouts, tool-call parsing, context compaction, loop detection, empty-reply retry, and a full overnight loop against a fake server. They do not load a GGUF, and they do not measure tokens per second.

Before trusting a long unattended job on the Legion:

1. `nvidia-smi` shows the 4070 and a current driver.
2. `.\scripts\setup.ps1` finishes, including both CUDA zips.
3. `.\scripts\smoke-test.ps1` passes (mock).
4. `.\scripts\tune-n-cpu-moe.ps1 -Profile quality` finishes and `nvidia-smi` during a successful candidate stays about 1 GB under the dedicated total.
5. `.\scripts\smoke-test.ps1 -Live -Profile quality` passes.
6. Then point `overnight.ps1` at a real repository.
