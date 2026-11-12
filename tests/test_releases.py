"""Windows CUDA asset selection, including the v0.9.0 nightly-tag indirection."""

from __future__ import annotations

import pytest

from local_agent.releases import (
    Asset,
    has_windows_cuda,
    pick_binary_release,
    select_windows_cuda_assets,
)

# Names observed on ggml-org/llama.cpp tag b12110 (llama.cpp 0.9.0, 2026-11-04).
B12110 = [
    Asset("nightly-tag.txt", "https://example.invalid/nightly", 7),
    Asset(
        "llama-b12110-bin-win-cuda-12.4-x64.zip",
        "https://example.invalid/llama-12.4",
        100,
    ),
    Asset(
        "cudart-llama-bin-win-cuda-12.4-x64.zip",
        "https://example.invalid/cudart-12.4",
        200,
    ),
    Asset(
        "llama-b12110-bin-win-cuda-13.4-x64.zip",
        "https://example.invalid/llama-13.4",
        110,
    ),
    Asset(
        "cudart-llama-bin-win-cuda-13.4-x64.zip",
        "https://example.invalid/cudart-13.4",
        210,
    ),
    Asset(
        "llama-b12110-bin-win-cuda-13.4-arm64.zip",
        "https://example.invalid/llama-arm",
        90,
    ),
    Asset("llama-b12110-bin-win-cpu-x64.zip", "https://example.invalid/cpu", 20),
]


def test_default_cuda_is_12_4_and_both_zips_are_required():
    plan = select_windows_cuda_assets(B12110, cuda="12.4", tag="b12110")
    assert plan.tag == "b12110"
    assert plan.cuda_version == "12.4"
    assert plan.llama_zip.name == "llama-b12110-bin-win-cuda-12.4-x64.zip"
    assert plan.cudart_zip.name == "cudart-llama-bin-win-cuda-12.4-x64.zip"


def test_latest_picks_the_highest_windows_x64_cuda():
    plan = select_windows_cuda_assets(B12110, cuda="latest", tag="b12110")
    assert plan.cuda_version == "13.4"
    assert plan.llama_zip.name.endswith("win-cuda-13.4-x64.zip")
    assert "arm64" not in plan.llama_zip.name


def test_missing_cuda_version_lists_what_exists():
    with pytest.raises(LookupError, match="13.1"):
        select_windows_cuda_assets(B12110, cuda="13.1", tag="b12110")


def test_version_tag_without_binaries_follows_nightly_tag():
    latest = {
        "tag_name": "v0.9.0",
        "assets": [{"name": "nightly-tag.txt", "browser_download_url": "https://example.invalid/n", "size": 7}],
    }
    nightly = {
        "tag_name": "b12110",
        "assets": [
            {"name": asset.name, "browser_download_url": asset.url, "size": asset.size} for asset in B12110
        ],
    }
    assert not has_windows_cuda(
        [Asset(item["name"], item["browser_download_url"]) for item in latest["assets"]]
    )
    chosen = pick_binary_release(latest, "b12110", nightly)
    assert chosen["tag_name"] == "b12110"
    plan = select_windows_cuda_assets(
        [Asset(item["name"], item["browser_download_url"], item["size"]) for item in chosen["assets"]],
        cuda="12.4",
        tag=chosen["tag_name"],
    )
    assert plan.llama_zip.url == "https://example.invalid/llama-12.4"
