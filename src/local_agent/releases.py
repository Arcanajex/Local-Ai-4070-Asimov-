"""Pick the Windows CUDA llama.cpp binaries from a GitHub release payload.

llama.cpp publishes version tags (v0.9.0) that often contain only a
nightly-tag.txt pointer. The actual zips live on the matching bNNNN tag.
Two zips are required: the llama tools, and the CUDA runtime DLLs. They
must be extracted into the same directory. CUDA 12.4 is the default because
it loads on any driver new enough for an RTX 4070 Laptop; a CUDA 13 build
needs a newer driver and is selected only when 12.4 is absent or requested.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Any

GITHUB_API = "https://api.github.com/repos/ggml-org/llama.cpp"
USER_AGENT = "local-coding-agent"
_CUDA_IN_NAME = re.compile(r"win-cuda-(\d+\.\d+)-x64\.zip$", re.IGNORECASE)


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int = 0


@dataclass(frozen=True)
class WindowsCudaPlan:
    tag: str
    cuda_version: str
    llama_zip: Asset
    cudart_zip: Asset

    def to_json(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "cuda_version": self.cuda_version,
            "llama_zip": {"name": self.llama_zip.name, "url": self.llama_zip.url, "size": self.llama_zip.size},
            "cudart_zip": {
                "name": self.cudart_zip.name,
                "url": self.cudart_zip.url,
                "size": self.cudart_zip.size,
            },
        }


def _assets(payload: dict[str, Any]) -> list[Asset]:
    found: list[Asset] = []
    for item in payload.get("assets") or []:
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        if name and url:
            found.append(Asset(name=name, url=url, size=int(item.get("size") or 0)))
    return found


def has_windows_cuda(assets: list[Asset]) -> bool:
    return any(_CUDA_IN_NAME.search(asset.name) for asset in assets)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def available_cuda_versions(assets: list[Asset]) -> list[str]:
    found: set[str] = set()
    for asset in assets:
        match = _CUDA_IN_NAME.search(asset.name)
        if match:
            found.add(match.group(1))
    return sorted(found, key=_version_key)


def select_windows_cuda_assets(
    assets: list[Asset],
    *,
    cuda: str = "12.4",
    tag: str = "",
) -> WindowsCudaPlan:
    """Choose the llama zip and the matching cudart zip.

    `cuda` is a version like "12.4". Pass "latest" to take the highest
    Windows x64 CUDA build in the release.
    """

    versions = available_cuda_versions(assets)
    if not versions:
        names = ", ".join(asset.name for asset in assets) or "(no assets)"
        raise LookupError(f"release {tag or '?'} has no Windows CUDA x64 zip. Assets: {names}")
    if cuda == "latest":
        chosen = versions[-1]
    elif cuda in versions:
        chosen = cuda
    else:
        raise LookupError(
            f"CUDA {cuda} is not in release {tag or '?'}. Available: {', '.join(versions)}"
        )
    suffix = f"win-cuda-{chosen}-x64.zip"
    llama = [
        asset
        for asset in assets
        if asset.name.lower().startswith("llama-") and asset.name.lower().endswith(suffix)
    ]
    cudart = [
        asset
        for asset in assets
        if asset.name.lower().startswith("cudart-") and asset.name.lower().endswith(suffix)
    ]
    if len(llama) != 1 or len(cudart) != 1:
        raise LookupError(
            f"expected one llama zip and one cudart zip for CUDA {chosen}, "
            f"found llama={[a.name for a in llama]} cudart={[a.name for a in cudart]}"
        )
    return WindowsCudaPlan(tag=tag, cuda_version=chosen, llama_zip=llama[0], cudart_zip=cudart[0])


def pick_binary_release(
    latest_payload: dict[str, Any],
    nightly_tag: str | None,
    nightly_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the release payload that actually contains Windows CUDA zips."""

    if has_windows_cuda(_assets(latest_payload)):
        return latest_payload
    if not nightly_tag or nightly_payload is None:
        raise LookupError(
            f"release {latest_payload.get('tag_name')} has no Windows CUDA assets "
            "and no nightly-tag.txt pointer was provided"
        )
    if not has_windows_cuda(_assets(nightly_payload)):
        raise LookupError(
            f"nightly tag {nightly_tag} (from {latest_payload.get('tag_name')}) "
            "has no Windows CUDA assets"
        )
    return nightly_payload


def _request(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def resolve_windows_cuda_plan(cuda: str = "12.4", timeout: int = 60) -> WindowsCudaPlan:
    """Hit the GitHub API and return the zip URLs to download."""

    latest = json.loads(_request(f"{GITHUB_API}/releases/latest", timeout=timeout))
    nightly_tag = None
    nightly_payload = None
    if not has_windows_cuda(_assets(latest)):
        pointer = (
            f"https://github.com/ggml-org/llama.cpp/releases/download/"
            f"{latest['tag_name']}/nightly-tag.txt"
        )
        nightly_tag = _request(pointer, timeout=timeout).decode("utf-8", errors="replace").strip()
        nightly_payload = json.loads(_request(f"{GITHUB_API}/releases/tags/{nightly_tag}", timeout=timeout))
    payload = pick_binary_release(latest, nightly_tag, nightly_payload)
    return select_windows_cuda_assets(_assets(payload), cuda=cuda, tag=str(payload.get("tag_name") or ""))
