"""Load model profiles and apply the tuner's local overrides."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_agent.paths import overrides_path, profiles_path

# Keys a user (or the tuner) may set in overrides.json. Anything else is a
# typo and should fail loudly instead of being ignored.
OVERRIDABLE = {
    "context",
    "n_predict",
    "n_cpu_moe",
    "n_cpu_ffn",
    "n_gpu_layers",
    "flash_attn",
    "batch_size",
    "ubatch_size",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "repeat_penalty",
    "reasoning",
    "reasoning_budget",
    "reasoning_effort",
    "agent_max_tokens",
    "agent_max_tokens_ceiling",
    "port",
    "host",
    "fit",
    "parallel",
    "cache_type_k",
    "cache_type_v",
}


@dataclass
class Profile:
    """One llama-server configuration plus the sampling the agent sends."""

    name: str
    role: str
    description: str
    optional: bool
    repo: str
    filename: str
    download_url: str
    size_bytes: int
    quant: str
    architecture: str
    moe: bool
    block_count: int
    context: int
    n_predict: int
    n_gpu_layers: str
    tune_candidates: list[int]
    reasoning: str
    temperature: float
    top_p: float
    top_k: int
    min_p: float
    repeat_penalty: float
    agent_max_tokens: int
    agent_max_tokens_ceiling: int
    host: str
    port: int
    parallel: int
    fit: str
    flash_attn: str
    cache_type_k: str
    cache_type_v: str
    batch_size: int
    ubatch_size: int
    jinja: bool
    timeout_seconds: int
    request_timeout_seconds: int
    n_cpu_moe: int | None = None
    n_cpu_ffn: int | None = None
    reasoning_budget: int | None = None
    reasoning_effort: str | None = None
    total_params_b: float | None = None
    active_params_b: float | None = None
    notes: str = ""
    expected: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def offload_flag(self) -> str:
        """Which offload knob this profile tunes."""

        return "n_cpu_moe" if self.moe else "n_cpu_ffn"

    @property
    def offload_value(self) -> int:
        value = self.n_cpu_moe if self.moe else self.n_cpu_ffn
        if value is None:
            raise ValueError(f"profile {self.name} has no {self.offload_flag}")
        return value


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    catalog_path = path or profiles_path()
    if not catalog_path.is_file():
        raise FileNotFoundError(f"profile catalog not found: {catalog_path}")
    return _load_json(catalog_path)


def load_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    override_file = path or overrides_path()
    if not override_file.is_file():
        return {}
    data = _load_json(override_file)
    cleaned: dict[str, dict[str, Any]] = {}
    for name, values in data.items():
        if name.startswith("_"):
            continue
        if not isinstance(values, dict):
            raise ValueError(f"override for {name} must be an object")
        unknown = sorted(set(values) - OVERRIDABLE)
        if unknown:
            raise ValueError(
                f"unknown override keys for {name}: {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(OVERRIDABLE))}"
            )
        cleaned[name] = values
    return cleaned


def _as_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number, got {value!r}")
    return int(value)


def profile_from_dict(name: str, data: dict[str, Any], defaults: dict[str, Any]) -> Profile:
    merged = {**defaults, **data}
    moe = bool(merged["moe"])
    return Profile(
        name=name,
        role=str(merged.get("role", name)),
        description=str(merged.get("description", "")),
        optional=bool(merged.get("optional", False)),
        repo=str(merged["repo"]),
        filename=str(merged["filename"]),
        download_url=str(merged["download_url"]),
        size_bytes=_as_int(merged["size_bytes"], "size_bytes"),
        quant=str(merged.get("quant", "")),
        architecture=str(merged.get("architecture", "")),
        moe=moe,
        block_count=_as_int(merged.get("block_count", 0), "block_count"),
        context=_as_int(merged["context"], "context"),
        n_predict=_as_int(merged.get("n_predict", -1), "n_predict"),
        n_gpu_layers=str(merged.get("n_gpu_layers", "all")),
        n_cpu_moe=(
            _as_int(merged["n_cpu_moe"], "n_cpu_moe") if merged.get("n_cpu_moe") is not None else None
        ),
        n_cpu_ffn=(
            _as_int(merged["n_cpu_ffn"], "n_cpu_ffn") if merged.get("n_cpu_ffn") is not None else None
        ),
        tune_candidates=[_as_int(item, "tune_candidates") for item in merged.get("tune_candidates", [])],
        reasoning=str(merged.get("reasoning", "auto")),
        reasoning_budget=(
            _as_int(merged["reasoning_budget"], "reasoning_budget")
            if merged.get("reasoning_budget") is not None
            else None
        ),
        reasoning_effort=(
            str(merged["reasoning_effort"]) if merged.get("reasoning_effort") else None
        ),
        temperature=float(merged.get("temperature", 0.6)),
        top_p=float(merged.get("top_p", 0.95)),
        top_k=_as_int(merged.get("top_k", 20), "top_k"),
        min_p=float(merged.get("min_p", 0.0)),
        repeat_penalty=float(merged.get("repeat_penalty", 1.0)),
        agent_max_tokens=_as_int(merged.get("agent_max_tokens", 8192), "agent_max_tokens"),
        agent_max_tokens_ceiling=_as_int(
            merged.get("agent_max_tokens_ceiling", merged.get("agent_max_tokens", 8192)),
            "agent_max_tokens_ceiling",
        ),
        host=str(merged.get("host", "127.0.0.1")),
        port=_as_int(merged.get("port", 8080), "port"),
        parallel=_as_int(merged.get("parallel", 1), "parallel"),
        fit=str(merged.get("fit", "off")),
        flash_attn=str(merged.get("flash_attn", "on")),
        cache_type_k=str(merged.get("cache_type_k", "q8_0")),
        cache_type_v=str(merged.get("cache_type_v", "q8_0")),
        batch_size=_as_int(merged.get("batch_size", 512), "batch_size"),
        ubatch_size=_as_int(merged.get("ubatch_size", 512), "ubatch_size"),
        jinja=bool(merged.get("jinja", True)),
        timeout_seconds=_as_int(merged.get("timeout_seconds", 3600), "timeout_seconds"),
        request_timeout_seconds=_as_int(
            merged.get("request_timeout_seconds", 3600), "request_timeout_seconds"
        ),
        total_params_b=(
            float(merged["total_params_b"]) if merged.get("total_params_b") is not None else None
        ),
        active_params_b=(
            float(merged["active_params_b"]) if merged.get("active_params_b") is not None else None
        ),
        notes=str(merged.get("notes", "")),
        expected=dict(merged.get("expected") or {}),
        raw=merged,
    )


def get_profile(
    name: str,
    *,
    catalog_path: Path | None = None,
    override_path: Path | None = None,
) -> Profile:
    catalog = load_catalog(catalog_path)
    profiles = catalog.get("profiles") or {}
    if name not in profiles:
        known = ", ".join(sorted(profiles))
        raise KeyError(f"unknown profile {name!r}. Known profiles: {known}")
    defaults = dict(catalog.get("server_defaults") or {})
    data = dict(profiles[name])
    overrides = load_overrides(override_path).get(name, {})
    data.update(overrides)
    return profile_from_dict(name, data, defaults)


def list_profiles(catalog_path: Path | None = None) -> list[str]:
    catalog = load_catalog(catalog_path)
    return list((catalog.get("profiles") or {}).keys())


def write_override(profile_name: str, values: dict[str, Any], path: Path | None = None) -> Path:
    """Merge keys into runtime/overrides.json without touching other profiles."""

    unknown = sorted(set(values) - OVERRIDABLE)
    if unknown:
        raise ValueError(f"unknown override keys: {', '.join(unknown)}")
    target = path or overrides_path()
    current: dict[str, Any] = {}
    if target.is_file():
        current = _load_json(target)
    profile_values = dict(current.get(profile_name) or {})
    profile_values.update(values)
    current[profile_name] = profile_values
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return target
