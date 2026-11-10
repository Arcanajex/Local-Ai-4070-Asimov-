"""Offload sweep policy and nvidia-smi parsing. No GPU is required."""

from __future__ import annotations

from local_agent.tune import (
    Trial,
    classify_server_failure,
    extract_tokens_per_second,
    parse_nvidia_smi,
    pick_gpu,
    select_best,
    should_stop_sweep,
    within_vram_cap,
)


def test_nvidia_smi_parser_picks_the_larger_gpu():
    text = "\n".join(
        [
            "0, NVIDIA GeForce RTX 4070 Laptop GPU, 512, 8188",
            "1, NVIDIA Something Small, 100, 2048",
        ]
    )
    samples = parse_nvidia_smi(text)
    assert len(samples) == 2
    chosen = pick_gpu(samples)
    assert chosen is not None
    assert chosen.total_mib == 8188
    assert "4070" in chosen.name
    assert within_vram_cap(7000, 8188, 1024)
    assert not within_vram_cap(7500, 8188, 1024)


def test_select_fastest_success_and_stop_at_the_cap():
    trials = [
        Trial(offload=40, ok=True, tokens_per_sec=20, vram_used_mib=4000),
        Trial(offload=34, ok=True, tokens_per_sec=28, vram_used_mib=6200),
        Trial(offload=30, ok=False, tokens_per_sec=40, vram_used_mib=7800, failure="vram_cap"),
    ]
    assert select_best(trials).offload == 34
    assert should_stop_sweep(trials[-1])
    # A measured slower run does not beat a faster one just because it uses more GPU.
    slower_gpu = [
        Trial(offload=40, ok=True, tokens_per_sec=30),
        Trial(offload=30, ok=True, tokens_per_sec=18),
    ]
    assert select_best(slower_gpu).offload == 40
    # With no timings, the most aggressive fit wins.
    untimed = [
        Trial(offload=40, ok=True),
        Trial(offload=34, ok=True),
    ]
    assert select_best(untimed).offload == 34


def test_oom_in_the_log_is_not_reported_as_a_generic_timeout():
    log = "llama_model_load: error loading model: cudaMalloc failed: out of memory"
    assert classify_server_failure(log, timed_out=True, returncode=None) == "oom"
    assert classify_server_failure("waiting", timed_out=True, returncode=None) == "timeout"


def test_tokens_per_second_from_llama_server_timings():
    assert extract_tokens_per_second({"timings": {"predicted_per_second": 35.5}}) == 35.5
    assert extract_tokens_per_second({}) is None
