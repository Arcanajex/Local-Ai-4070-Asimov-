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

JSON object values stay in this list so `server-args` can still print them.
`split_json_args` moves them into `LLAMA_ARG_*` environment variables at
launch. Windows PowerShell 5.1 strips embedded double quotes from native
arguments, and llama-server then fails to parse the JSON.
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
    # Qwen templates keep prior `reasoning_content` only when the template is
    # told to. gpt-oss does not use this key; leave it off there. The flag
    # name is `--chat-template-kwargs` (a JSON object) on llama.cpp b12110.
    # Launchers pass it as LLAMA_ARG_CHAT_TEMPLATE_KWARGS; see split_json_args.
    if profile.preserve_thinking:
        args.extend(
            [
                "--chat-template-kwargs",
                json.dumps({"preserve_thinking": True}, separators=(",", ":")),
            ]
        )
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


# llama.cpp b12110 names this env var in `--help`. The mechanical
# LLAMA_ARG_<FLAG> form matches it; the map pins the verified name.
_JSON_ENV_NAMES = {
    "--chat-template-kwargs": "LLAMA_ARG_CHAT_TEMPLATE_KWARGS",
}


def llama_arg_env_name(flag: str) -> str:
    """Environment variable llama-server uses for `flag` (b12110 set_env)."""

    if flag in _JSON_ENV_NAMES:
        return _JSON_ENV_NAMES[flag]
    if not flag.startswith("--"):
        raise ValueError(f"not a llama-server flag: {flag}")
    return "LLAMA_ARG_" + flag[2:].replace("-", "_").upper()


def _json_object_or_array(value: str) -> bool:
    stripped = value.lstrip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, (dict, list))


def split_json_args(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Move JSON-valued flags into llama-server environment variables.

    Windows PowerShell 5.1 strips embedded double quotes when it builds a
    native command line (`& $exe @args`, and Start-Process). llama-server
    then reads `{preserve_thinking:true}` and exits with
    json.exception.parse_error.101 at `{p`. The same setting is read from
    the environment (`LLAMA_ARG_CHAT_TEMPLATE_KWARGS` on llama.cpp b12110),
    which never goes through that quoting. pwsh 7 uses the same launch path.

    The input list is not modified. Keys in the env dict are insertion order.
    """

    argv: list[str] = []
    env: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        value = args[index + 1] if index + 1 < len(args) else None
        takes_json = token in _JSON_ENV_NAMES or (
            token.startswith("--") and value is not None and _json_object_or_array(value)
        )
        if takes_json:
            if value is None or not _json_object_or_array(value):
                raise ValueError(f"{token} is missing a JSON object or array value")
            env[llama_arg_env_name(token)] = value
            index += 2
            continue
        argv.append(token)
        index += 1
    return argv, env


def format_args(args: list[str], style: str) -> str:
    if style == "json":
        return json.dumps(args)
    if style == "lines":
        return "\n".join(args)
    if style == "launch":
        argv, env = split_json_args(args)
        return json.dumps({"args": argv, "env": env})
    raise ValueError(f"unknown format {style!r}")
