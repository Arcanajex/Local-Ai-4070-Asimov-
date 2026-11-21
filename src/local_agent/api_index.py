"""Look up public NuGet API from XML docs, with a DLL fallback.

The index is built from the target project's obj/project.assets.json (and
from PackageReference items when restore has not written that file). XML doc
files are read from the NuGet cache. A package with no XML docs falls back to
public signatures in the assembly metadata. Each package version is cached as
JSON so the next run does not parse it again.

lookup_api and the fix-prompt injection both call lookup(). Results are
trimmed to a character budget so they stay small next to a 32K prompt.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from local_agent.dotnet_meta import MetadataError, read_public_api
from local_agent.paths import runtime_dir

DEFAULT_API_LOOKUP = True
DEFAULT_API_LOOKUP_BUDGET = 1600
MIN_API_LOOKUP_BUDGET = 200
MAX_API_LOOKUP_BUDGET = 4000
_MAX_HITS = 8
_SUMMARY_CHARS = 180

# Member and signature errors. CS0214 stays on the cheat-sheet list only:
# it is an unsafe-context error, not a missing member.
LOOKUP_CODES: tuple[str, ...] = (
    "CS1061",
    "CS0117",
    "CS7036",
    "CS1503",
    "CS1501",
    "CS0122",
    "CS0246",
)

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "bin",
    "node_modules",
    ".venv",
    "venv",
    "packages",
    ".agent",
    "__pycache__",
}
_DOC_PRIMITIVE = {
    "System.Void": "void",
    "System.Boolean": "bool",
    "System.Char": "char",
    "System.SByte": "sbyte",
    "System.Byte": "byte",
    "System.Int16": "short",
    "System.UInt16": "ushort",
    "System.Int32": "int",
    "System.UInt32": "uint",
    "System.Int64": "long",
    "System.UInt64": "ulong",
    "System.Single": "float",
    "System.Double": "double",
    "System.String": "string",
    "System.Object": "object",
    "System.Decimal": "decimal",
    "System.IntPtr": "nint",
    "System.UIntPtr": "nuint",
}
_TYPE_PARAM = ("T", "U", "V", "W")
_CS1061 = re.compile(
    r"CS1061:.*?'(?P<type>[^']+)' does not contain a definition for '(?P<member>[^']+)'",
    re.IGNORECASE,
)
_CS0117 = re.compile(
    r"CS0117:.*?'(?P<type>[^']+)' does not contain a definition for '(?P<member>[^']+)'",
    re.IGNORECASE,
)
_CS7036 = re.compile(
    r"CS7036:.*?parameter '(?P<param>[^']+)' of '(?P<signature>[^']+)'",
    re.IGNORECASE,
)
_CS1501 = re.compile(r"CS1501:.*?method '(?P<member>[^']+)'", re.IGNORECASE)
_CS1503 = re.compile(
    r"CS1503:.*?cannot convert from '(?P<src>[^']+)' to '(?P<dst>[^']+)'",
    re.IGNORECASE,
)
_CS0122 = re.compile(r"CS0122:.*?'(?P<member>[^']+)' is inaccessible", re.IGNORECASE)
_CS0246 = re.compile(r"CS0246:.*?name '(?P<type>[^']+)' could not be found", re.IGNORECASE)
_MEMBER_BLOCK = re.compile(r'<member\s+name="([^"]+)"\s*>(.*?)</member>', re.IGNORECASE | re.DOTALL)
_SUMMARY_BLOCK = re.compile(r"<summary>(.*?)</summary>", re.IGNORECASE | re.DOTALL)
_RETURNS_BLOCK = re.compile(r"<returns>(.*?)</returns>", re.IGNORECASE | re.DOTALL)
_PARAM_BLOCK = re.compile(
    r'<param\s+name="([^"]+)"\s*(?:/>|>(.*?)</param>)',
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ApiLookupSettings:
    enabled: bool
    budget: int


@dataclass(frozen=True)
class ApiMember:
    member_id: str
    kind: str
    name: str
    parent: str
    signature: str
    summary: str
    package: str
    version: str
    source: str


@dataclass
class ApiIndex:
    members: list[ApiMember] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    logged: bool = False


@dataclass(frozen=True)
class LookupResult:
    text: str
    hits: int
    query: str


@dataclass(frozen=True)
class PackageRef:
    package_id: str
    version: str
    relative_path: str
    xml_rel: str
    dll_rel: str
    folders: tuple[Path, ...]


def resolve_api_lookup_settings(
    task_enabled: bool | None,
    task_budget: int | None,
    *,
    profile_enabled: bool = DEFAULT_API_LOOKUP,
    profile_budget: int = DEFAULT_API_LOOKUP_BUDGET,
) -> ApiLookupSettings:
    """Task front matter wins, then the profile. Default on, 1600 characters.

    `apply_project_defaults` copies AGENTS.md onto the task first.
    """

    enabled = profile_enabled if task_enabled is None else bool(task_enabled)
    budget = profile_budget if task_budget is None else int(task_budget)
    if budget <= 0:
        budget = DEFAULT_API_LOOKUP_BUDGET
    budget = max(MIN_API_LOOKUP_BUDGET, min(int(budget), MAX_API_LOOKUP_BUDGET))
    return ApiLookupSettings(enabled=enabled, budget=budget)


def output_needs_api_lookup(output: str) -> bool:
    text = output or ""
    return any(code in text for code in LOOKUP_CODES)


def compiler_queries(output: str) -> list[str]:
    """Type or member names named by member/signature compiler errors.

    The order is the order the errors appear in the build output.
    """

    text = output or ""
    found_at: list[tuple[int, str]] = []

    def take(match: re.Match[str], query: str) -> None:
        cleaned = " ".join((query or "").split())
        if cleaned:
            found_at.append((match.start(), cleaned))

    for match in _CS1061.finditer(text):
        take(match, f"{match.group('type')}.{match.group('member')}")
    for match in _CS0117.finditer(text):
        take(match, f"{match.group('type')}.{match.group('member')}")
    for match in _CS7036.finditer(text):
        take(match, match.group("signature").split("(", 1)[0].strip())
    for match in _CS1501.finditer(text):
        take(match, match.group("member"))
    for match in _CS1503.finditer(text):
        take(match, _short(match.group("dst")))
    for match in _CS0122.finditer(text):
        take(match, match.group("member"))
    for match in _CS0246.finditer(text):
        take(match, match.group("type"))
    found_at.sort(key=lambda item: item[0])
    found: list[str] = []
    seen: set[str] = set()
    for _pos, query in found_at:
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(query)
    return found


def trim_text(text: str, budget: int) -> str:
    """Keep whole lines when they fit. The result is at most `budget` characters."""

    body = text or ""
    limit = max(1, int(budget))
    if len(body) <= limit:
        return body
    note = "\n[trimmed]"
    if limit <= len(note):
        return body[:limit]
    keep = limit - len(note)
    cut = body.rfind("\n", 0, keep + 1)
    if cut < max(1, keep // 3):
        cut = keep
    return body[:cut].rstrip() + note


def default_nuget_roots() -> list[Path]:
    """NUGET_PACKAGES, then the user cache. The cache is not required to exist."""

    roots: list[Path] = []
    env = os.environ.get("NUGET_PACKAGES")
    if env and env.strip():
        roots.append(Path(env).expanduser())
    home = Path.home()
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE")
        base = Path(profile).expanduser() if profile else home
    else:
        base = home
    roots.append(base / ".nuget" / "packages")
    return _unique_paths(roots)


def discover_packages(project: Path) -> list[PackageRef]:
    """Packages referenced by project.assets.json, then by PackageReference."""

    root = Path(project)
    found: list[PackageRef] = []
    seen: set[tuple[str, str]] = set()
    if not root.is_dir():
        return found
    for assets in _walk_files(root, "project.assets.json"):
        for ref in _packages_from_assets(assets):
            key = (ref.package_id.casefold(), ref.version)
            if key in seen:
                continue
            seen.add(key)
            found.append(ref)
    for project_file in _project_files(root):
        for ref in _packages_from_project(project_file):
            key = (ref.package_id.casefold(), ref.version)
            if key in seen:
                continue
            seen.add(key)
            found.append(ref)
    return found


def build_api_index(
    project: Path,
    *,
    cache_dir: Path | None = None,
    nuget_roots: list[Path] | None = None,
) -> ApiIndex:
    """Index every referenced package. Missing docs are warnings, not errors."""

    cache = Path(cache_dir) if cache_dir is not None else runtime_dir() / "api-index"
    extra = _unique_paths([*(nuget_roots or []), *nuget_config_folders(Path(project)), *default_nuget_roots()])
    index = ApiIndex()
    for ref in discover_packages(project):
        label = f"{ref.package_id} {ref.version}"
        index.packages.append(label)
        located = _locate_package(ref, extra)
        xml_path, dll_path = _package_files(located, ref)
        members, warning = _members_for_package(ref, xml_path, dll_path, cache)
        if warning:
            index.warnings.append(warning)
        index.members.extend(members)
    return index


def ensure_index(workspace: Any, logger: Any = None) -> ApiIndex:
    """Build the project's index once. Warnings are logged the first time."""

    cached = getattr(workspace, "_api_index", None)
    if cached is None:
        cached = build_api_index(
            workspace.root,
            cache_dir=getattr(workspace, "api_cache_dir", None),
            nuget_roots=getattr(workspace, "nuget_roots", None),
        )
        workspace._api_index = cached
    if logger is not None and not cached.logged:
        for warning in cached.warnings:
            logger.event("api_index", summary=warning[:500])
        logger.event(
            "api_index",
            packages=len(cached.packages),
            members=len(cached.members),
            summary=f"api_index packages={len(cached.packages)} members={len(cached.members)}",
        )
        cached.logged = True
    return cached


def lookup(index: ApiIndex, query: str, *, budget: int = DEFAULT_API_LOOKUP_BUDGET) -> LookupResult:
    """Signatures for a type, a member, a doc id, or a fuzzy name."""

    text_query = " ".join((query or "").split())
    if not text_query:
        return LookupResult("Give lookup_api a type name, a member name, or Type.Member.", 0, "")
    if not index.members:
        return LookupResult(
            "No NuGet package API is indexed for this project. "
            "Restore packages so obj/project.assets.json and the NuGet cache are present.",
            0,
            text_query,
        )
    selected, suggestions, note = _select(index, text_query)
    body, hits = _format(selected, note=note, suggestions=suggestions, budget=budget)
    if not body:
        body = f"No matching public API for {text_query}."
    return LookupResult(trim_text(body, budget), hits, text_query)


def autolookup(index: ApiIndex, output: str, *, budget: int = DEFAULT_API_LOOKUP_BUDGET) -> LookupResult:
    """Look up every type or member named by compiler errors in `output`."""

    queries = compiler_queries(output)
    if not queries:
        return LookupResult("", 0, "")
    if not index.members:
        return LookupResult("", 0, ", ".join(queries))
    parts: list[str] = []
    hits = 0
    for query in queries:
        result = lookup(index, query, budget=budget)
        if result.hits and result.text:
            parts.append(result.text)
            hits += result.hits
    if not parts:
        # Still surface closest names when the compiler named a member we do not have.
        for query in queries:
            result = lookup(index, query, budget=budget)
            if result.text and "No matching" not in result.text and "No NuGet" not in result.text:
                parts.append(result.text)
        text = trim_text("\n\n".join(parts), budget)
        return LookupResult(text, 0, ", ".join(queries))
    return LookupResult(trim_text("\n\n".join(parts), budget), hits, ", ".join(queries))


def parse_doc_xml(text: str) -> list[dict[str, Any]]:
    """Public members from one XML doc file. Generics, overloads, and nested types.

    Each record has member_id, kind, name, parent, signature, summary.
    """

    raw = text.lstrip("\ufeff")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return _parse_doc_regex(raw)
    records = []
    type_params = _type_param_names(root)
    for element in root.iter():
        if _local(element.tag) != "member":
            continue
        member_id = (element.get("name") or "").strip()
        if not member_id or ":" not in member_id:
            continue
        record = _record_from_id(member_id, element, type_params)
        if record is not None:
            records.append(record)
    return records


def nuget_config_folders(project: Path) -> list[Path]:
    """globalPackagesFolder from nuget.config, walking from the project upward."""

    folders: list[Path] = []
    current = Path(project)
    if current.is_file():
        current = current.parent
    seen: set[Path] = set()
    while current not in seen:
        seen.add(current)
        for name in ("nuget.config", "NuGet.Config", "NuGet.config"):
            config = current / name
            if config.is_file():
                folders.extend(_read_global_packages(config))
        if current.parent == current:
            break
        current = current.parent
    return folders


def _members_for_package(
    ref: PackageRef,
    xml_path: Path | None,
    dll_path: Path | None,
    cache: Path,
) -> tuple[list[ApiMember], str]:
    source_path = xml_path if xml_path and xml_path.is_file() else None
    source_kind = "xml"
    if source_path is None and dll_path and dll_path.is_file():
        source_path = dll_path
        source_kind = "dll"
    stamp = _stamp(source_path) if source_path is not None else None
    cached = _read_cache(cache, ref, stamp)
    if cached is not None:
        return cached, ""
    if xml_path is not None and xml_path.is_file():
        try:
            parsed = parse_doc_xml(xml_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError) as exc:
            parsed = []
            xml_error = str(exc)
        else:
            xml_error = ""
        if parsed:
            members = [_member_from_record(ref, item, "xml") for item in parsed]
            _write_cache(cache, ref, _stamp(xml_path), members)
            return members, ""
        if dll_path is None or not dll_path.is_file():
            detail = xml_error or "XML docs were empty"
            return [], f"{ref.package_id} {ref.version}: no usable XML docs ({detail})"
    if dll_path is not None and dll_path.is_file():
        try:
            meta = read_public_api(dll_path)
        except MetadataError as exc:
            return [], f"{ref.package_id} {ref.version}: no XML docs; metadata read failed: {exc}"
        except OSError as exc:
            return [], f"{ref.package_id} {ref.version}: no XML docs; metadata read failed: {exc}"
        members = [
            ApiMember(
                member_id=item.member_id,
                kind=item.kind,
                name=item.name,
                parent=item.parent,
                signature=item.signature,
                summary="",
                package=ref.package_id,
                version=ref.version,
                source="dll",
            )
            for item in meta
        ]
        _write_cache(cache, ref, _stamp(dll_path), members)
        return (
            members,
            f"{ref.package_id} {ref.version}: no XML docs; read {len(members)} public members from {dll_path.name}",
        )
    if source_kind == "xml":
        return [], f"{ref.package_id} {ref.version}: XML docs could not be read and no assembly was found"
    return [], f"{ref.package_id} {ref.version}: no XML docs and no assembly under the NuGet cache"


def _member_from_record(ref: PackageRef, record: dict[str, Any], source: str) -> ApiMember:
    return ApiMember(
        member_id=record["member_id"],
        kind=record["kind"],
        name=record["name"],
        parent=record["parent"],
        signature=record["signature"],
        summary=record.get("summary") or "",
        package=ref.package_id,
        version=ref.version,
        source=source,
    )


def _select(index: ApiIndex, query: str) -> tuple[list[ApiMember], list[str], str]:
    exact = [item for item in index.members if item.member_id.casefold() == query.casefold()]
    if exact:
        return exact[:_MAX_HITS], [], ""
    type_part, member_part = _split_query(index, query)
    if type_part and member_part:
        types = _types_named(index, type_part)
        pool = _members_of(index, types) if types else []
        owned = [item for item in pool if _name_is(item, member_part) and item.kind != "type"]
        if not owned:
            owned = [
                item
                for item in index.members
                if _name_is(item, member_part) and _parent_matches(item, type_part)
            ]
        if owned:
            return _with_nested_members(index, owned)[:_MAX_HITS], [], ""
        names = _suggestion_pool(pool or index.members)
        suggestions = _closest(member_part, names)
        suggested = _members_named(pool or index.members, suggestions)
        note = f"No member {member_part!r} on {type_part}."
        return suggested[:_MAX_HITS], suggestions, note
    types = _types_named(index, query)
    by_name = [item for item in index.members if _name_is(item, query)]
    if types or by_name:
        selected: list[ApiMember] = []
        seen: set[str] = set()
        for item in [*types, *by_name]:
            if item.member_id in seen:
                continue
            seen.add(item.member_id)
            selected.append(item)
            if item.kind == "type":
                for child in _members_of(index, [item]):
                    if child.member_id in seen or child.kind == "type":
                        continue
                    seen.add(child.member_id)
                    selected.append(child)
                    if len(selected) >= _MAX_HITS:
                        break
            if len(selected) >= _MAX_HITS:
                break
        return selected[:_MAX_HITS], [], ""
    folded = query.casefold()
    substring = [
        item
        for item in index.members
        if folded in item.name.casefold() or folded in item.signature.casefold()
    ]
    if substring:
        return substring[:_MAX_HITS], [], ""
    suggestions = _closest(query, _suggestion_pool(index.members))
    suggested = _members_named(index.members, suggestions)
    note = f"No exact match for {query!r}."
    return suggested[:_MAX_HITS], suggestions, note


def _format(
    members: list[ApiMember],
    *,
    note: str,
    suggestions: list[str],
    budget: int,
) -> tuple[str, int]:
    lines: list[str] = []
    if note:
        lines.append(note)
    current = ""
    shown = 0
    for member in members:
        header = f"{member.package} {member.version}"
        if member.source == "dll":
            header += " (assembly metadata)"
        if header != current:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(header)
            current = header
        line = member.signature
        if member.kind != "type":
            line = "  " + line
        if member.summary:
            line += " - " + member.summary
        lines.append(line)
        shown += 1
    if suggestions:
        lines.append("Closest: " + ", ".join(suggestions))
    return trim_text("\n".join(lines).strip(), budget), shown


def _split_query(index: ApiIndex, query: str) -> tuple[str, str]:
    head = query.split("(", 1)[0].strip()
    if "." not in head:
        return "", head
    parts = head.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        type_name = ".".join(parts[:cut])
        member_name = ".".join(parts[cut:])
        if _types_named(index, type_name):
            return type_name, member_name
    return ".".join(parts[:-1]), parts[-1]


def _with_nested_members(index: ApiIndex, owned: list[ApiMember]) -> list[ApiMember]:
    """A nested type named by the query includes that type's members."""

    expanded: list[ApiMember] = []
    seen: set[str] = set()
    for item in owned:
        if item.member_id in seen:
            continue
        seen.add(item.member_id)
        expanded.append(item)
        if item.kind != "type":
            continue
        for child in _members_of(index, [item]):
            if child.member_id in seen or child.kind == "type":
                continue
            seen.add(child.member_id)
            expanded.append(child)
    return expanded


def _types_named(index: ApiIndex, query: str) -> list[ApiMember]:
    folded = query.casefold()
    found = []
    for item in index.members:
        if item.kind != "type":
            continue
        names = {
            item.name.casefold(),
            item.signature.casefold(),
            item.member_id[2:].casefold() if item.member_id[1:2] == ":" else item.member_id.casefold(),
            _short(item.signature).casefold(),
        }
        if folded in names:
            found.append(item)
    return found


def _members_of(index: ApiIndex, types: list[ApiMember]) -> list[ApiMember]:
    if not types:
        return []
    keys = set()
    for item in types:
        body = item.member_id[2:] if item.member_id[1:2] == ":" else item.signature
        keys.add(body.casefold())
        keys.add(item.signature.casefold())
        keys.add(item.name.casefold())
    return [item for item in index.members if item.parent.casefold() in keys or _parent_key(item) in keys]


def _parent_key(member: ApiMember) -> str:
    return _friendly_dotted(member.parent).casefold()


def _parent_matches(member: ApiMember, type_part: str) -> bool:
    folded = type_part.casefold()
    parent = member.parent.casefold()
    friendly = _friendly_dotted(member.parent).casefold()
    return folded in {parent, friendly, _short(member.parent).casefold(), _short(friendly).casefold()}


def _name_is(member: ApiMember, member_part: str) -> bool:
    folded = member_part.casefold()
    short = _short_identifier(member_part.split("(", 1)[0]).casefold()
    return folded in {member.name.casefold(), member.member_id.casefold()} or short == member.name.casefold()


def _suggestion_pool(members: list[ApiMember]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in members:
        if item.kind == "type" or not item.name or item.name.startswith("#"):
            continue
        if item.name.casefold() in seen:
            continue
        seen.add(item.name.casefold())
        names.append(item.name)
    return names


def _closest(query: str, names: list[str]) -> list[str]:
    if not names or not query:
        return []
    folded = {name.casefold(): name for name in names}
    prefix = [name for name in names if name.casefold().startswith(query.casefold())]
    if prefix:
        return prefix[:5]
    matches = difflib.get_close_matches(query, list(folded), n=5, cutoff=0.55)
    return [folded.get(match.casefold(), match) for match in matches]


def _members_named(members: list[ApiMember], names: list[str]) -> list[ApiMember]:
    wanted = {name.casefold() for name in names}
    return [item for item in members if item.name.casefold() in wanted and item.kind != "type"]


def _packages_from_assets(path: Path) -> list[PackageRef]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    folders = []
    for folder in (data.get("packageFolders") or {}):
        candidate = Path(str(folder))
        if candidate.is_dir():
            folders.append(candidate)
    tfms = _project_tfms(data)
    refs: list[PackageRef] = []
    for key, lib in (data.get("libraries") or {}).items():
        if not isinstance(lib, dict) or lib.get("type") != "package" or "/" not in str(key):
            continue
        package_id, version = str(key).split("/", 1)
        relative = str(lib.get("path") or f"{package_id.lower()}/{version}").replace("\\", "/")
        files = [str(item).replace("\\", "/") for item in lib.get("files") or []]
        xml_rel, dll_rel = _pick_compile_files(data, str(key), files, tfms)
        refs.append(
            PackageRef(
                package_id=package_id,
                version=version,
                relative_path=relative,
                xml_rel=xml_rel,
                dll_rel=dll_rel,
                folders=tuple(folders),
            )
        )
    return refs


def _packages_from_project(path: Path) -> list[PackageRef]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return []
    refs: list[PackageRef] = []
    for element in root.iter():
        if _local(element.tag) != "PackageReference":
            continue
        package_id = (element.get("Include") or element.get("Update") or "").strip()
        version = (element.get("Version") or "").strip()
        if not version:
            for child in list(element):
                if _local(child.tag) == "Version" and (child.text or "").strip():
                    version = child.text.strip()
        if not package_id or not version or any(token in version for token in "*[(]"):
            continue
        refs.append(
            PackageRef(
                package_id=package_id,
                version=version,
                relative_path=f"{package_id.lower()}/{version}",
                xml_rel="",
                dll_rel="",
                folders=(),
            )
        )
    return refs


def _pick_compile_files(
    data: dict[str, Any],
    library_key: str,
    files: list[str],
    tfms: list[str],
) -> tuple[str, str]:
    dlls: list[str] = []
    for target, libraries in (data.get("targets") or {}).items():
        if not isinstance(libraries, dict) or library_key not in libraries:
            continue
        lib = libraries[library_key]
        if not isinstance(lib, dict):
            continue
        rank = _tfm_rank(str(target), tfms)
        for bucket in ("compile", "runtime"):
            group = lib.get(bucket) or {}
            if not isinstance(group, dict):
                continue
            for rel in group:
                text = str(rel).replace("\\", "/")
                if text.lower().endswith(".dll"):
                    dlls.append((rank, text))
    dlls.sort()
    dll_rel = dlls[0][1] if dlls else ""
    if not dll_rel:
        candidates = [item for item in files if item.lower().endswith(".dll") and "/lib/" in item.lower()]
        candidates.sort(key=lambda item: _path_rank(item, tfms))
        dll_rel = candidates[0] if candidates else ""
    xml_rel = ""
    if dll_rel:
        sibling = str(Path(dll_rel).with_suffix(".xml")).replace("\\", "/")
        if not files or sibling in files or sibling.lower() in {item.lower() for item in files}:
            xml_rel = sibling
    if not xml_rel:
        xmls = [item for item in files if item.lower().endswith(".xml") and "/lib/" in item.lower()]
        xmls.sort(key=lambda item: _path_rank(item, tfms))
        xml_rel = xmls[0] if xmls else ""
    return xml_rel, dll_rel


def _project_tfms(data: dict[str, Any]) -> list[str]:
    frameworks = (data.get("project") or {}).get("frameworks") or {}
    if isinstance(frameworks, dict):
        return [str(name) for name in frameworks]
    return []


def _tfm_rank(target: str, preferred: list[str]) -> tuple[int, int]:
    """Match `net8.0` to a NuGet target such as `.NETCoreApp,Version=v8.0`."""

    lowered = target.lower().replace(" ", "")
    for index, tfm in enumerate(preferred):
        token = tfm.lower()
        version = token.removeprefix("net")
        if version.isdigit() and len(version) == 2:
            version = version[0] + "." + version[1]
        if token in lowered or (version and f"v{version}" in lowered):
            return (0, index)
    return (1, 0)


def _path_rank(path: str, preferred: list[str]) -> tuple[int, int, str]:
    lowered = path.lower()
    for index, tfm in enumerate(preferred):
        if f"/{tfm.lower()}/" in lowered:
            return (0, index, lowered)
    return (1, 0, lowered)


def _locate_package(ref: PackageRef, roots: list[Path]) -> Path | None:
    relatives = [ref.relative_path] if ref.relative_path else []
    relatives.append(f"{ref.package_id.lower()}/{ref.version}")
    for root in [*ref.folders, *roots]:
        for relative in relatives:
            candidate = root / relative
            if candidate.is_dir():
                return candidate
    return None


def _package_files(directory: Path | None, ref: PackageRef) -> tuple[Path | None, Path | None]:
    if directory is None:
        return None, None
    xml_path = directory / ref.xml_rel if ref.xml_rel else None
    dll_path = directory / ref.dll_rel if ref.dll_rel else None
    if xml_path is None or not xml_path.is_file():
        xml_path = _first_match(directory, ".xml")
    if dll_path is None or not dll_path.is_file():
        dll_path = _first_match(directory, ".dll")
    return xml_path, dll_path


def _first_match(directory: Path, suffix: str) -> Path | None:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [name for name in dirnames if name not in {".git"}]
        for name in filenames:
            if name.lower().endswith(suffix) and "lib" in Path(dirpath).parts:
                found.append(Path(dirpath) / name)
    if not found:
        return None
    found.sort(key=_lib_sort_key)
    return found[0]


def _lib_sort_key(path: Path) -> tuple[int, int, int, str]:
    """Prefer a newer net TFM over an older one, and over netstandard."""

    name = path.parent.name.lower()
    match = re.search(r"net(?:standard)?(\d+)(?:\.(\d+))?", name)
    if not match:
        return (2, 0, 0, str(path).lower())
    standard = 1 if "standard" in name else 0
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return (standard, -major, -minor, str(path).lower())


def _cache_path(cache: Path, ref: PackageRef) -> Path:
    package = re.sub(r"[^A-Za-z0-9._-]+", "_", ref.package_id.lower())
    version = re.sub(r"[^A-Za-z0-9._-]+", "_", ref.version)
    return cache / package / f"{version}.json"


def _stamp(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    stat = path.stat()
    return f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}"


def _read_cache(cache: Path, ref: PackageRef, stamp: str | None) -> list[ApiMember] | None:
    path = _cache_path(cache, ref)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if stamp is not None and data.get("stamp") != stamp:
        return None
    rows = data.get("members")
    if not isinstance(rows, list):
        return None
    members: list[ApiMember] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        try:
            members.append(ApiMember(**row))
        except TypeError:
            return None
    return members


def _write_cache(cache: Path, ref: PackageRef, stamp: str | None, members: list[ApiMember]) -> None:
    path = _cache_path(cache, ref)
    payload = {
        "package": ref.package_id,
        "version": ref.version,
        "stamp": stamp,
        "members": [asdict(item) for item in members],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return


def _record_from_id(
    member_id: str,
    element: ET.Element | None,
    type_params: dict[str, list[str]],
) -> dict[str, Any] | None:
    kind, parent, name, args = _split_member_id(member_id)
    if not kind or not name:
        return None
    short = _short_identifier(name)
    param_names = _param_names(element)
    summary = _summary(element)
    signature = _friendly_signature(kind, parent, name, args, param_names, type_params)
    return {
        "member_id": member_id,
        "kind": {"T": "type", "M": "method", "F": "field", "P": "property", "E": "event"}.get(kind, "member"),
        "name": short,
        "parent": parent,
        "signature": signature,
        "summary": summary,
    }


def _friendly_signature(
    kind: str,
    parent: str,
    name: str,
    args: list[str],
    param_names: list[str],
    type_params: dict[str, list[str]],
) -> str:
    if kind == "T":
        full = f"{parent}.{name}" if parent else name
        return _friendly_dotted(full, type_params.get(full, []))
    owner = _friendly_dotted(parent, type_params.get(parent, []))
    method = _friendly_dotted(name)
    if kind == "M":
        rendered = []
        for index, arg in enumerate(args):
            shown = friendly_type(arg)
            if index < len(param_names) and param_names[index]:
                shown = f"{shown} {param_names[index]}"
            rendered.append(shown)
        head = f"{owner}.{method}" if owner else method
        if name == "#ctor":
            head = owner or method
        return f"{head}({', '.join(rendered)})"
    head = f"{owner}.{_short_identifier(name)}" if owner else _short_identifier(name)
    return head


def friendly_type(text: str) -> str:
    """Turn a doc-id type such as System.Int32 or ``0 into a short signature."""

    shown, _pos = _scan_type(text.strip(), 0)
    return shown


def _scan_type(text: str, index: int) -> tuple[str, int]:
    if index >= len(text):
        return "", index
    if text.startswith("``", index):
        number, index = _read_number(text, index + 2)
        return _tparam(number, method=True), index
    if text.startswith("`", index):
        number, index = _read_number(text, index + 1)
        return _tparam(number, method=False), index
    start = index
    while index < len(text) and text[index] not in "`{[*@,}]":
        index += 1
    name = text[start:index]
    if index < len(text) and text[index] == "`" and not text.startswith("``", index):
        _arity, index = _read_number(text, index + 1)
    args: list[str] = []
    if index < len(text) and text[index] == "{":
        args, index = _scan_list(text, index + 1, "}")
    elif text.startswith("[[", index):
        args, index = _scan_list(text, index + 2, "]]")
    suffix = ""
    byref = False
    while index < len(text):
        if text[index] == "[":
            end = text.find("]", index)
            if end < 0:
                break
            inside = text[index + 1 : end]
            if inside == "":
                suffix += "[]"
            elif inside and all(char in "0:, " for char in inside):
                suffix += "[" + ("," * max(0, inside.count(","))) + "]"
            else:
                suffix += text[index : end + 1]
            index = end + 1
            continue
        if text[index] == "*":
            suffix += "*"
            index += 1
            continue
        if text[index] == "@":
            byref = True
            index += 1
            continue
        break
    base = _DOC_PRIMITIVE.get(name, name)
    if args:
        base = f"{base.split('`', 1)[0]}<{', '.join(args)}>"
    shown = base + suffix
    if byref:
        shown = "ref " + shown
    return shown, index


def _scan_list(text: str, index: int, closing: str) -> tuple[list[str], int]:
    args: list[str] = []
    while index < len(text) and not text.startswith(closing, index):
        if text[index] == ",":
            index += 1
            continue
        if text[index].isspace():
            index += 1
            continue
        shown, index = _scan_type(text, index)
        # Assembly-qualified names continue after a comma inside [[ ]].
        if closing == "]]":
            while index < len(text) and not text.startswith("]]", index) and text[index] != ",":
                index += 1
            if index < len(text) and text[index] == ",":
                while index < len(text) and not text.startswith("]]", index):
                    index += 1
        if shown:
            args.append(shown)
    index += len(closing) if text.startswith(closing, index) else 0
    return args, index


def _read_number(text: str, index: int) -> tuple[int, int]:
    start = index
    while index < len(text) and text[index].isdigit():
        index += 1
    if start == index:
        return 0, index
    return int(text[start:index]), index


def _friendly_dotted(text: str, type_param_names: list[str] | None = None) -> str:
    if not text:
        return ""
    parts = []
    for part in text.split("."):
        method = re.match(r"^(.*?)``(\d+)$", part)
        if method:
            count = int(method.group(2))
            labels = ", ".join(_tparam(index, method=True) for index in range(count))
            parts.append(f"{method.group(1)}<{labels}>")
            continue
        typed = re.match(r"^(.*?)`(\d+)$", part)
        if typed:
            count = int(typed.group(2))
            names = type_param_names or []
            labels = ", ".join(names[index] if index < len(names) and names[index] else _tparam(index, False) for index in range(count))
            parts.append(f"{typed.group(1)}<{labels}>")
            continue
        parts.append(part)
    return ".".join(parts)


def _tparam(number: int, method: bool) -> str:
    if method:
        return f"T{number + 1}"
    if 0 <= number < len(_TYPE_PARAM):
        return _TYPE_PARAM[number]
    return f"T{number + 1}"


def _split_member_id(member_id: str) -> tuple[str, str, str, list[str]]:
    if len(member_id) < 3 or member_id[1] != ":":
        return "", "", "", []
    kind = member_id[0].upper()
    body = member_id[2:]
    args: list[str] = []
    head = body
    if "(" in body:
        head, rest = body.split("(", 1)
        rest = rest[:-1] if rest.endswith(")") else rest
        args = _split_top_level(rest) if rest.strip() else []
    if "." in head:
        parent, name = head.rsplit(".", 1)
    else:
        parent, name = "", head
    return kind, parent, name, args


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char in "{[(<":
            depth += 1
        elif char in "}])>":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _short_identifier(name: str) -> str:
    name = re.sub(r"``\d+$", "", name)
    name = re.sub(r"`\d+$", "", name)
    return name


def _short(text: str) -> str:
    friendly = _friendly_dotted(text)
    return friendly.rsplit(".", 1)[-1]


def _summary(element: ET.Element | None) -> str:
    if element is None:
        return ""
    chunks: list[str] = []
    for child in list(element):
        tag = _local(child.tag)
        if tag == "summary":
            chunks.append(_inline(child))
        elif tag == "returns":
            text = _inline(child)
            if text:
                chunks.append("Returns " + text)
    return _collapse(" ".join(chunks))[:_SUMMARY_CHARS]


def _param_names(element: ET.Element | None) -> list[str]:
    if element is None:
        return []
    names = []
    for child in list(element):
        if _local(child.tag) == "param" and (child.get("name") or "").strip():
            names.append(child.get("name", "").strip())
    return names


def _type_param_names(root: ET.Element) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for element in root.iter():
        if _local(element.tag) != "member":
            continue
        member_id = (element.get("name") or "").strip()
        if not member_id.startswith("T:"):
            continue
        names = [
            (child.get("name") or "").strip()
            for child in list(element)
            if _local(child.tag) == "typeparam" and (child.get("name") or "").strip()
        ]
        if names:
            found[member_id[2:]] = names
    return found


def _inline(element: ET.Element) -> str:
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in list(element):
        tag = _local(child.tag)
        if tag in {"see", "seealso"}:
            parts.append(_cref_name(child.get("cref") or "") or (child.text or ""))
        elif tag in {"paramref", "typeparamref"}:
            parts.append(child.get("name") or "")
        else:
            parts.append(_inline(child))
        if child.tail:
            parts.append(child.tail)
    return _collapse("".join(parts))


def _cref_name(cref: str) -> str:
    if ":" in cref:
        cref = cref.split(":", 1)[1]
    cref = cref.split("(", 1)[0]
    return _short_identifier(cref.rsplit(".", 1)[-1])


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_doc_regex(text: str) -> list[dict[str, Any]]:
    records = []
    for member_id, body in _MEMBER_BLOCK.findall(text):
        summary = ""
        match = _SUMMARY_BLOCK.search(body)
        if match:
            summary = _collapse(re.sub(r"<[^>]+>", " ", match.group(1)))
        returns = _RETURNS_BLOCK.search(body)
        if returns:
            extra = _collapse(re.sub(r"<[^>]+>", " ", returns.group(1)))
            if extra:
                summary = _collapse(summary + " Returns " + extra)
        names = [name for name, _inner in _PARAM_BLOCK.findall(body)]
        # Build a tiny element so the shared formatter can see param names.
        element = ET.Element("member", {"name": member_id})
        if summary:
            node = ET.SubElement(element, "summary")
            node.text = summary
        for name in names:
            ET.SubElement(element, "param", {"name": name})
        record = _record_from_id(member_id, element, {})
        if record is not None:
            records.append(record)
    return records


def _read_global_packages(path: Path) -> list[Path]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return []
    folders = []
    for element in root.iter():
        if _local(element.tag) != "add":
            continue
        if (element.get("key") or "").lower() != "globalpackagesfolder":
            continue
        value = (element.get("value") or "").strip()
        if not value:
            continue
        folder = Path(value).expanduser()
        if not folder.is_absolute():
            folder = (path.parent / folder).resolve()
        folders.append(folder)
    return folders


def _walk_files(root: Path, name: str) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [item for item in dirnames if item not in _SKIP_DIRS and not item.startswith(".")]
        if name in filenames and Path(dirpath).name == "obj":
            found.append(Path(dirpath) / name)
    return found


def _project_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [item for item in dirnames if item not in _SKIP_DIRS and item != "obj"]
        for name in filenames:
            if name.lower().endswith((".csproj", ".fsproj", ".vbproj")):
                found.append(Path(dirpath) / name)
    return found


def _local(tag: str) -> str:
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _unique_paths(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(Path(path))
        folded = key.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        found.append(Path(path))
    return found
