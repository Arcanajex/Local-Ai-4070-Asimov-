"""Profile catalog and the llama-server argument list."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.config import get_profile, write_override
from local_agent.server_args import build_server_args


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def test_quality_profile_matches_checked_gguf():
    profile = get_profile("quality")
    assert profile.filename == "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
    assert profile.repo == "unsloth/Qwen3.6-35B-A3B-GGUF"
    assert profile.size_bytes == 22134528992
    assert profile.moe is True
    assert profile.context == 40960
    assert profile.n_cpu_moe == 34
    assert profile.n_predict == -1
    assert profile.agent_max_tokens >= 8192
    assert profile.agent_max_tokens_ceiling >= profile.agent_max_tokens
    assert profile.reasoning == "on"
    assert 32768 <= profile.context <= 40960


def test_day_profile_matches_the_8gb_bench():
    profile = get_profile("day")
    assert profile.filename == "gpt-oss-20b-MXFP4.gguf"
    assert profile.repo == "ggml-org/gpt-oss-20b-GGUF"
    assert profile.size_bytes == 12109566624
    assert profile.context == 32768
    assert profile.n_cpu_moe == 30
    assert profile.block_count == 24
    assert profile.agent_max_tokens == 8192
    assert profile.reasoning_effort == "medium"
    assert profile.jinja is True


def test_max_profile_is_optional_dense_offload():
    profile = get_profile("max")
    assert profile.optional is True
    assert profile.moe is False
    assert profile.filename == "Qwen3.8-27B-Q4_K_M.gguf"
    assert profile.size_bytes == 19403684480
    assert profile.n_cpu_ffn == 64
    assert profile.n_cpu_moe is None
    assert profile.context == 16384
    assert profile.reasoning_effort == "high"


def test_server_args_pin_the_laptop_safe_flags():
    quality = get_profile("quality")
    args = build_server_args(quality, model_file=Path("Qwen.gguf"))
    assert _flag(args, "--model") == "Qwen.gguf"
    assert _flag(args, "--host") == "127.0.0.1"
    assert _flag(args, "--ctx-size") == "40960"
    assert _flag(args, "--n-predict") == "-1"
    assert _flag(args, "--n-gpu-layers") == "all"
    assert _flag(args, "--parallel") == "1"
    assert _flag(args, "--fit") == "off"
    assert _flag(args, "--flash-attn") == "on"
    assert _flag(args, "--cache-type-k") == "q8_0"
    assert _flag(args, "--n-cpu-moe") == "34"
    assert _flag(args, "--reasoning") == "on"
    assert _flag(args, "--reasoning-budget") == "-1"
    assert "--jinja" in args
    assert "--n-cpu-ffn" not in args

    day = build_server_args(get_profile("day"), model_file=Path("gpt.gguf"))
    assert _flag(day, "--n-cpu-moe") == "30"
    assert _flag(day, "--ctx-size") == "32768"
    assert _flag(day, "--reasoning-effort") == "medium"
    assert "--reasoning-budget" not in day

    dense = build_server_args(get_profile("max"), model_file=Path("dense.gguf"))
    assert _flag(dense, "--n-cpu-ffn") == "64"
    assert "--n-cpu-moe" not in dense
    assert _flag(dense, "--reasoning-effort") == "high"


def test_overrides_replace_offload_and_reject_typos(tmp_path):
    path = tmp_path / "overrides.json"
    write_override("quality", {"n_cpu_moe": 40, "context": 32768}, path)
    profile = get_profile("quality", override_path=path)
    assert profile.n_cpu_moe == 40
    assert profile.context == 32768
    assert profile.filename == "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
    with pytest.raises(ValueError, match="unknown override"):
        write_override("quality", {"n_cpu_moee": 10}, path)


def test_unknown_profile():
    with pytest.raises(KeyError, match="unknown profile"):
        get_profile("nope")
