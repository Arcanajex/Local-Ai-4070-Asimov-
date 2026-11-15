"""Profile catalog and the llama-server argument list."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_agent.config import get_profile, write_override
from local_agent.server_args import build_server_args, format_args, split_json_args


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def test_quality_profile_matches_checked_gguf():
    profile = get_profile("quality")
    assert profile.filename == "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
    assert profile.repo == "unsloth/Qwen3.6-35B-A3B-GGUF"
    assert profile.size_bytes == 22134528992
    assert profile.moe is True
    assert profile.context == 65536
    assert profile.n_cpu_moe == 34
    assert profile.n_predict == -1
    assert profile.agent_max_tokens >= 8192
    assert profile.agent_max_tokens_ceiling >= profile.agent_max_tokens
    assert profile.reasoning == "on"
    assert profile.reasoning_budget == 10240
    assert profile.preserve_thinking is True
    assert profile.reasoning_replay_turns == 2
    assert profile.temperature == 0.6
    assert profile.top_p == 0.95
    assert profile.top_k == 20
    assert profile.repeat_penalty == 1.0


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
    assert profile.preserve_thinking is False
    assert profile.reasoning_replay_turns == 0


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
    assert _flag(args, "--ctx-size") == "65536"
    assert _flag(args, "--n-predict") == "-1"
    assert _flag(args, "--n-gpu-layers") == "all"
    assert _flag(args, "--parallel") == "1"
    assert _flag(args, "--fit") == "off"
    assert _flag(args, "--flash-attn") == "on"
    assert _flag(args, "--cache-type-k") == "q8_0"
    assert _flag(args, "--n-cpu-moe") == "34"
    assert _flag(args, "--reasoning") == "on"
    assert _flag(args, "--reasoning-budget") == "10240"
    assert "--jinja" in args
    assert "--n-cpu-ffn" not in args
    assert _flag(args, "--chat-template-kwargs") == '{"preserve_thinking":true}'

    day = build_server_args(get_profile("day"), model_file=Path("gpt.gguf"))
    assert _flag(day, "--n-cpu-moe") == "30"
    assert _flag(day, "--ctx-size") == "32768"
    assert _flag(day, "--reasoning-effort") == "medium"
    assert "--reasoning-budget" not in day
    assert "--chat-template-kwargs" not in day

    dense = build_server_args(get_profile("max"), model_file=Path("dense.gguf"))
    assert _flag(dense, "--n-cpu-ffn") == "64"
    assert "--n-cpu-moe" not in dense
    assert _flag(dense, "--reasoning-effort") == "high"
    assert _flag(dense, "--chat-template-kwargs") == '{"preserve_thinking":true}'


def test_split_json_args_moves_objects_into_llama_env_vars():
    """PowerShell 5.1 strips quotes inside a native argument. The env value keeps them."""

    original = [
        "--model",
        "Qwen.gguf",
        "--chat-template-kwargs",
        '{"preserve_thinking":true}',
        "--jinja",
        "--mcp-servers-json",
        '{"servers":[]}',
    ]
    argv, env = split_json_args(original)
    assert original[2] == "--chat-template-kwargs"
    assert argv == ["--model", "Qwen.gguf", "--jinja"]
    assert env == {
        "LLAMA_ARG_CHAT_TEMPLATE_KWARGS": '{"preserve_thinking":true}',
        "LLAMA_ARG_MCP_SERVERS_JSON": '{"servers":[]}',
    }
    assert '"' in env["LLAMA_ARG_CHAT_TEMPLATE_KWARGS"]


def test_quality_and_max_launch_with_preserve_thinking_in_the_environment():
    for name in ("quality", "max"):
        args = build_server_args(get_profile(name), model_file=Path(f"{name}.gguf"))
        argv, env = split_json_args(args)
        assert "--chat-template-kwargs" not in argv
        assert env == {"LLAMA_ARG_CHAT_TEMPLATE_KWARGS": '{"preserve_thinking":true}'}
        payload = json.loads(format_args(args, "launch"))
        assert payload["args"] == argv
        assert payload["env"] == env
        # The logical argv still shows the flag. Launch is the process hand-off.
        assert "--chat-template-kwargs" in json.loads(format_args(args, "json"))


def test_day_profile_launch_has_no_json_env():
    day = build_server_args(get_profile("day"), model_file=Path("gpt.gguf"))
    argv, env = split_json_args(day)
    assert argv == day
    assert env == {}
    payload = json.loads(format_args(day, "launch"))
    assert payload == {"args": day, "env": {}}


def test_split_json_args_leaves_plain_tokens_and_rejects_a_bare_flag():
    argv, env = split_json_args(["--alias", "{not-json", "--n-predict", "-1"])
    assert argv == ["--alias", "{not-json", "--n-predict", "-1"]
    assert env == {}
    with pytest.raises(ValueError, match="chat-template-kwargs"):
        split_json_args(["--jinja", "--chat-template-kwargs"])
    with pytest.raises(ValueError, match="JSON"):
        split_json_args(["--chat-template-kwargs", "--jinja"])


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
