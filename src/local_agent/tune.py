"""Pick the fastest expert/FFN offload that stays inside VRAM.

Higher `--n-cpu-moe` (or `--n-cpu-ffn` on the dense profile) keeps more
weights in system RAM, which is slower and safer. The sweep walks the
profile's candidate list from safest to most aggressive and stops at the
first out-of-memory or over-cap result. Among the runs that stayed under
the cap, the fastest measured decode wins. If the server did not report
tokens per second, the most aggressive run that still fit wins, which is
the same choice on this hardware: more of the model on the GPU is faster
until it isn't.

Windows will allocate past the dedicated VRAM and then crawl. A run that
loads is not a success unless `nvidia-smi` memory.used stays at least
`vram_headroom_mib` below memory.total. The default headroom is 1024 MiB.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from local_agent.client import server_is_healthy
from local_agent.config import Profile, get_profile, write_override
from local_agent.paths import llama_server_path
from local_agent.server_args import build_server_args, model_path
from local_agent.tools import kill_process_tree

OOM_MARKERS = (
    "out of memory",
    "failed to allocate",
    "cudamalloc",
    "cuda error: out of memory",
)


@dataclass
class GpuSample:
    index: int
    name: str
    used_mib: int
    total_mib: int


@dataclass
class Trial:
    offload: int
    ok: bool
    tokens_per_sec: float | None = None
    vram_used_mib: int | None = None
    vram_total_mib: int | None = None
    failure: str = ""
    detail: str = ""


def parse_nvidia_smi(text: str) -> list[GpuSample]:
    """Parse `nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader,nounits`."""

    samples: list[GpuSample] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            samples.append(
                GpuSample(
                    index=int(parts[0]),
                    name=",".join(parts[1:-2]).strip(),
                    used_mib=int(float(parts[-2])),
                    total_mib=int(float(parts[-1])),
                )
            )
        except ValueError:
            continue
    return samples


def pick_gpu(samples: list[GpuSample]) -> GpuSample | None:
    if not samples:
        return None
    return max(samples, key=lambda sample: sample.total_mib)


def within_vram_cap(used_mib: int, total_mib: int, headroom_mib: int) -> bool:
    return used_mib <= max(0, total_mib - headroom_mib)


def classify_server_failure(log_text: str, *, timed_out: bool, returncode: int | None) -> str:
    lowered = log_text.lower()
    # OOM is more useful than "timed out" when the process was killed by the driver
    # while we were still waiting for /health.
    if any(marker in lowered for marker in OOM_MARKERS):
        return "oom"
    if timed_out:
        return "timeout"
    if returncode not in (0, None):
        return "crash"
    return "unhealthy"


def select_best(trials: list[Trial]) -> Trial | None:
    """Fastest successful trial. Missing tok/s loses to a measured one, then lower offload wins."""

    good = [trial for trial in trials if trial.ok]
    if not good:
        return None

    def sort_key(trial: Trial) -> tuple[float, int]:
        speed = trial.tokens_per_sec if trial.tokens_per_sec is not None else -1.0
        return (speed, -trial.offload)

    return max(good, key=sort_key)


def should_stop_sweep(trial: Trial) -> bool:
    """Stop once a riskier offload is unlikely to fit. Candidates are safest-first."""

    return (not trial.ok) and trial.failure in {"oom", "vram_cap", "crash", "timeout"}


def profile_with_offload(profile: Profile, offload: int) -> Profile:
    if profile.moe:
        return replace(profile, n_cpu_moe=offload)
    return replace(profile, n_cpu_ffn=offload)


def query_nvidia_smi() -> list[GpuSample]:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed:\n{result.stdout}")
    return parse_nvidia_smi(result.stdout)


def extract_tokens_per_second(payload: dict[str, Any]) -> float | None:
    timings = payload.get("timings") or {}
    for key in ("predicted_per_second", "predicted_per_token_ms"):
        if key in timings and key.endswith("per_second"):
            try:
                return float(timings[key])
            except (TypeError, ValueError):
                return None
    if "predicted_per_second" in payload:
        try:
            return float(payload["predicted_per_second"])
        except (TypeError, ValueError):
            return None
    return None


def run_sweep(
    profile: Profile,
    *,
    llama_exe: Path | None = None,
    headroom_mib: int = 1024,
    load_timeout: int = 300,
    log_dir: Path | None = None,
    port: int | None = None,
) -> Trial | None:
    """Load the model at each candidate offload and record the winner.

    This starts and kills llama-server. Stop any server already using the port.
    """

    exe = llama_exe or llama_server_path()
    if not exe.is_file():
        raise FileNotFoundError(
            f"llama-server not found at {exe}. Run scripts/setup.ps1 on Windows first."
        )
    weights = model_path(profile)
    if not weights.is_file():
        raise FileNotFoundError(f"model file not found: {weights}")
    base_port = port if port is not None else profile.port
    base_url = f"http://{profile.host}:{base_port}"
    if server_is_healthy(base_url):
        raise RuntimeError(
            f"something is already serving {base_url}. Stop it before tuning so the "
            "VRAM sample is this process and not another one."
        )
    samples = query_nvidia_smi()
    gpu = pick_gpu(samples)
    if gpu is None:
        raise RuntimeError("nvidia-smi returned no GPU. This tuner needs the NVIDIA driver.")
    directory = log_dir or (weights.parent.parent / "logs" / "tune" / profile.name)
    directory.mkdir(parents=True, exist_ok=True)
    trials: list[Trial] = []
    for offload in profile.tune_candidates:
        trial = probe_offload(
            profile,
            offload,
            exe=exe,
            base_url=base_url,
            port=base_port,
            gpu_total_mib=gpu.total_mib,
            headroom_mib=headroom_mib,
            load_timeout=load_timeout,
            log_path=directory / f"offload-{offload}.log",
        )
        trials.append(trial)
        (directory / "trials.json").write_text(
            json.dumps([trial.__dict__ for trial in trials], indent=2) + "\n",
            encoding="utf-8",
        )
        if should_stop_sweep(trial):
            break
    winner = select_best(trials)
    if winner is not None:
        write_override(profile.name, {profile.offload_flag: winner.offload})
    return winner


def probe_offload(
    profile: Profile,
    offload: int,
    *,
    exe: Path,
    base_url: str,
    port: int,
    gpu_total_mib: int,
    headroom_mib: int,
    load_timeout: int,
    log_path: Path,
) -> Trial:
    tuned = profile_with_offload(profile, offload)
    argv = build_server_args(tuned, port=port)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(exe), *argv],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    timed_out = False
    try:
        if not _wait_healthy(base_url, load_timeout):
            timed_out = True
            log_handle.flush()
            text = log_path.read_text(encoding="utf-8", errors="replace")
            failure = classify_server_failure(text, timed_out=True, returncode=proc.poll())
            return Trial(offload=offload, ok=False, failure=failure, detail=text[-2000:])
        try:
            speed = _sample_completion(base_url, profile)
        except Exception as exc:  # noqa: BLE001 - the log text is the useful diagnostic
            log_handle.flush()
            text = log_path.read_text(encoding="utf-8", errors="replace")
            failure = classify_server_failure(text, timed_out=False, returncode=proc.poll())
            if failure == "unhealthy":
                failure = "crash"
            return Trial(offload=offload, ok=False, failure=failure, detail=f"{exc}\n{text[-1500:]}")
        samples = query_nvidia_smi()
        gpu = pick_gpu(samples)
        used = gpu.used_mib if gpu else None
        total = gpu.total_mib if gpu else gpu_total_mib
        if used is None:
            return Trial(offload=offload, ok=False, failure="unhealthy", tokens_per_sec=speed, detail="no GPU sample")
        fits = within_vram_cap(used, total, headroom_mib)
        # A missing tokens/sec figure still counts as a fit. select_best then
        # prefers whichever successful offload puts more of the model on the GPU.
        return Trial(
            offload=offload,
            ok=fits,
            tokens_per_sec=speed,
            vram_used_mib=used,
            vram_total_mib=total,
            failure="" if fits else "vram_cap",
            detail="" if fits else f"used {used} MiB, cap {total - headroom_mib} MiB",
        )
    finally:
        kill_process_tree(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()


def _wait_healthy(base_url: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_is_healthy(base_url, timeout=3):
            return True
        time.sleep(2)
    return False


def _sample_completion(base_url: str, profile: Profile) -> float | None:
    body = json.dumps(
        {
            "model": profile.filename,
            "messages": [{"role": "user", "content": "Reply with the single word ok."}],
            "max_tokens": 16,
            "temperature": 0,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return extract_tokens_per_second(payload)


def sweep_profile(name: str, **kwargs: Any) -> Trial | None:
    return run_sweep(get_profile(name), **kwargs)
