"""Build the llama-server argument list for a profile.

The list is the source of truth for the PowerShell launcher. Flags that are
easy to get wrong on an 8 GB laptop are set on purpose:

* `--fit off` so a pinned context is not silently reduced.
* `--n-predict -1` so a thinking model's token budget is the request's
  max_tokens, not a small server default. A short cap returns empty content
  because the reasoning trace consumes the whole budget.
* `--parallel 1` so the KV cache is not multiplied by extra slots.
* `--host 127.0.0.1` so the OpenAI-compatible API stays on the machine.
* `--jinja` even though recent llama-server builds default it on. Tool
  calling depends on the GGUF chat template.
"""

from __future__ import annotations

import json
from pathlib import Path

from local_agent.config import Profile, get_profile
from local_agent.paths import models_dir


def model_path(profile: Profile, models_root: Path | None = None) -> Path:
    return (models_root or models_dir()) / profile.filename


def build_server_args(
    profile: Profile,
    *,
    model_file: Path | None = None,
    host: str | None = None,
    port: int | None = None,
) -> list[str]:
    """Return argv for llama-server, without the executable path."""

    path = model_file or model_path(profile)
    args: list[str] = [
        "--model",
        str(path),
        "--host",
        host or profile.host,
        "--port",
        str(port if port is not None else profile.port),
        "--ctx-size",
        str(profile.context),
        "--n-predict",
        str(profile.n_predict),
        "--n-gpu-layers",
        str(profile.n_gpu_layers),
        "--parallel",
        str(profile.parallel),
        "--fit",
        profile.fit,
        "--flash-attn",
        profile.flash_attn,
        "--cache-type-k",
        profile.cache_type_k,
        "--cache-type-v",
        profile.cache_type_v,
        "--batch-size",
        str(profile.batch_size),
        "--ubatch-size",
        str(profile.ubatch_size),
        "--timeout",
        str(profile.timeout_seconds),
        "--reasoning",
        profile.reasoning,
        "--temp",
        _num(profile.temperature),
        "--top-p",
        _num(profile.top_p),
        "--top-k",
        str(profile.top_k),
        "--min-p",
        _num(profile.min_p),
        "--repeat-penalty",
        _num(profile.repeat_penalty),
    ]
    if profile.jinja:
        args.append("--jinja")
    if profile.moe:
        if profile.n_cpu_moe is None:
            raise ValueError(f"MoE profile {profile.name} is missing n_cpu_moe")
        args.extend(["--n-cpu-moe", str(profile.n_cpu_moe)])
    elif profile.n_cpu_ffn is not None:
        args.extend(["--n-cpu-ffn", str(profile.n_cpu_ffn)])
    if profile.reasoning_budget is not None:
        args.extend(["--reasoning-budget", str(profile.reasoning_budget)])
    if profile.reasoning_effort:
        args.extend(["--reasoning-effort", profile.reasoning_effort])
    return args


def _num(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text else "0"


def args_for_profile(
    name: str,
    *,
    allow_missing_model: bool = False,
    host: str | None = None,
    port: int | None = None,
) -> list[str]:
    profile = get_profile(name)
    path = model_path(profile)
    if not path.is_file() and not allow_missing_model:
        raise FileNotFoundError(
            f"model file not found: {path}\n"
            f"Download it with scripts/download-models.ps1 -Models {name} "
            "(native Windows PowerShell, not WSL)."
        )
    return build_server_args(profile, model_file=path, host=host, port=port)


def format_args(args: list[str], style: str) -> str:
    if style == "json":
        return json.dumps(args)
    if style == "lines":
        return "\n".join(args)
    raise ValueError(f"unknown format {style!r}")
