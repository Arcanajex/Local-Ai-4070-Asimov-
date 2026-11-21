"""A compact symbol index of the target repo.

The map is injected once, into the first user message of a phase. Compaction
keeps that message, so the map is not copied into later turns. The overnight
runner rebuilds it before later phases (after the plan names files, and after
edits) instead of appending a second copy.

C# is outlined with a small lexer (strings and comments are masked, then
namespaces, types, methods, and fields are read from the code). Python uses
the stdlib ast module. Other source files get a short regex fallback. No
tree-sitter or Roslyn dependency: the laptop only needs Python, and the
Windows CI job stays dependency-free. Roslyn would require the dotnet SDK,
which is not assumed.

bin/, obj/, .git, build outputs, vendored directories, and .gitignore
matches are skipped. The index is ranked for the task and cut to a character
budget. The default budget is 4,000 tokens (16,000 characters), inside the
3–6k band, and the resolver clamps a configured budget to 6,000 tokens so
the map cannot crowd the 32,768-token message cap.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO_MAP_TOKENS = 4_000
REPO_MAP_TOKENS_MAX = 6_000
CHARS_PER_TOKEN = 4
REPO_MAP_CHARS = REPO_MAP_TOKENS * CHARS_PER_TOKEN
MAP_MARKER = "<!-- asimov-repo-map -->"

SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".agent",
    ".vs",
    ".idea",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "bower_components",
    "packages",
    "vendor",
    "third_party",
    "third-party",
    "external",
    "runtime",
    "dist",
    "build",
    "bin",
    "obj",
    "out",
    "target",
    "artifacts",
    "testresults",
    "coverage",
    "htmlcov",
}

SOURCE_SUFFIXES = {
    ".cs",
    ".csx",
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".fs",
    ".fsx",
    ".vb",
    ".cpp",
    ".cc",
    ".cxx",
    ".h",
    ".hpp",
    ".hh",
    ".c",
    ".csproj",
    ".fsproj",
    ".vbproj",
    ".sln",
}

_MAX_SCAN = 2_500
_MAX_READ = 400_000
_LINE_CAP = 340

_CS_MODIFIERS = (
    "public",
    "private",
    "protected",
    "internal",
    "static",
    "abstract",
    "sealed",
    "partial",
    "readonly",
    "ref",
    "unsafe",
    "new",
    "file",
    "virtual",
    "override",
    "extern",
    "async",
    "volatile",
    "const",
    "required",
    "event",
    "fixed",
    "implicit",
    "explicit",
)
_CS_KEYWORDS = {
    "if",
    "for",
    "foreach",
    "while",
    "switch",
    "catch",
    "lock",
    "using",
    "fixed",
    "when",
    "return",
    "new",
    "sizeof",
    "typeof",
    "nameof",
    "default",
    "checked",
    "unchecked",
    "where",
    "get",
    "set",
    "init",
    "add",
    "remove",
    "else",
    "do",
    "try",
    "throw",
    "yield",
    "await",
    "base",
    "this",
    "var",
    "and",
    "or",
    "not",
    "class",
    "struct",
    "interface",
    "enum",
    "record",
    "namespace",
    "void",
}
_TYPE_KINDS = {"class", "struct", "interface", "enum", "record"}
_GENERIC_FALLBACK = {
    ".js": (
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)"),
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?class[ \t]+([A-Za-z_]\w*)"),
    ),
    ".jsx": (
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)"),
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?class[ \t]+([A-Za-z_]\w*)"),
    ),
    ".mjs": (
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)"),
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?class[ \t]+([A-Za-z_]\w*)"),
    ),
    ".ts": (
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)"),
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?(?:abstract[ \t]+)?class[ \t]+([A-Za-z_]\w*)"),
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?interface[ \t]+([A-Za-z_]\w*)"),
    ),
    ".tsx": (
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)"),
        re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?class[ \t]+([A-Za-z_]\w*)"),
    ),
    ".go": (
        re.compile(r"(?m)^func[ \t]+(?:\([^)]*\)[ \t]+)?([A-Za-z_]\w*)[ \t]*\(([^)]*)\)"),
        re.compile(r"(?m)^type[ \t]+([A-Za-z_]\w*)[ \t]+(?:struct|interface)"),
    ),
    ".rs": (
        re.compile(r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?(?:async[ \t]+)?fn[ \t]+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)"),
        re.compile(r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?(?:struct|enum|trait)[ \t]+([A-Za-z_]\w*)"),
    ),
    ".java": (
        re.compile(r"(?m)^[ \t]*(?:public|private|protected)?[ \t]*(?:class|interface|enum)[ \t]+([A-Za-z_]\w*)"),
        re.compile(
            r"(?m)^[ \t]*(?:public|private|protected|static|final|synchronized|native)[ \t]+"
            r"(?:[\w.<>,\[\]?]+[ \t]+)+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)"
        ),
    ),
    ".fs": (
        re.compile(r"(?m)^[ \t]*(?:let|member|type)[ \t]+([A-Za-z_]\w*)"),
    ),
    ".fsx": (
        re.compile(r"(?m)^[ \t]*(?:let|member|type)[ \t]+([A-Za-z_]\w*)"),
    ),
    ".cpp": (
        re.compile(r"(?m)^[ \t]*(?:[\w:<>,\*&]+[ \t]+)+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)[ \t]*\{"),
    ),
    ".cc": (
        re.compile(r"(?m)^[ \t]*(?:[\w:<>,\*&]+[ \t]+)+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)[ \t]*\{"),
    ),
    ".cxx": (
        re.compile(r"(?m)^[ \t]*(?:[\w:<>,\*&]+[ \t]+)+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)[ \t]*\{"),
    ),
    ".h": (
        re.compile(r"(?m)^[ \t]*(?:class|struct)[ \t]+([A-Za-z_]\w*)"),
    ),
    ".hpp": (
        re.compile(r"(?m)^[ \t]*(?:class|struct)[ \t]+([A-Za-z_]\w*)"),
    ),
    ".c": (
        re.compile(r"(?m)^[ \t]*(?:[\w\*&]+[ \t]+)+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)[ \t]*\{"),
    ),
}
_USING = re.compile(r"(?m)^\s*using\s+(?:static\s+)?([A-Za-z_][\w.]*)\s*;")
_TASK_WORD = re.compile(r"[A-Za-z_][\w./\\-]{2,}")
_TASK_STOP = {
    "this",
    "that",
    "with",
    "from",
    "task",
    "file",
    "files",
    "when",
    "then",
    "true",
    "false",
    "none",
    "goal",
    "acceptance",
    "criteria",
    "scope",
    "should",
    "must",
    "into",
    "your",
    "have",
    "does",
    "will",
    "the",
    "and",
    "for",
    "not",
    "are",
    "was",
    "were",
    "been",
    "being",
    "each",
    "only",
    "than",
    "them",
    "they",
    "what",
    "which",
    "while",
    "where",
    "after",
    "before",
    "about",
    "there",
    "their",
    "would",
    "could",
    "class",
    "method",
    "function",
    "return",
    "public",
    "private",
    "static",
    "void",
    "string",
    "using",
    "namespace",
}


@dataclass
class RepoMapResult:
    text: str
    scanned: int
    included: int
    truncated: bool

    @property
    def tokens(self) -> int:
        if not self.text:
            return 0
        return max(1, len(self.text) // CHARS_PER_TOKEN)


@dataclass
class _Outline:
    rel: str
    line: str
    short: str
    names: set[str] = field(default_factory=set)
    refs: set[str] = field(default_factory=set)
    mtime: float = 0.0


@dataclass
class _IgnoreRule:
    base: str
    regex: re.Pattern[str]
    dir_only: bool
    negated: bool
    anchored: bool


class _IgnoreRules:
    """A practical subset of .gitignore: comments, directory slashes, *, ?, **."""

    def __init__(self) -> None:
        self.rules: list[_IgnoreRule] = []
        self._loaded: set[str] = set()

    def add_file(self, path: Path, base: str) -> None:
        key = base.strip("/")
        if key in self._loaded:
            return
        self._loaded.add(key)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negated = False
            if line.startswith("!"):
                negated = True
                line = line[1:].strip()
            if not line or line.startswith("!"):
                continue
            dir_only = line.endswith("/")
            if dir_only:
                line = line[:-1]
            if line.startswith("/"):
                line = line[1:]
                anchored = True
            else:
                anchored = "/" in line
            regex = _compile_gitignore(line)
            if regex is None:
                continue
            self.rules.append(_IgnoreRule(key, regex, dir_only, negated, anchored))

    def ignored(self, rel: str, *, is_dir: bool) -> bool:
        rel = rel.strip("/")
        if not rel:
            return False
        matched = False
        for rule in self.rules:
            if rule.dir_only and not is_dir:
                continue
            rest = rel
            if rule.base:
                prefix = rule.base + "/"
                if rel == rule.base:
                    continue
                if not rel.startswith(prefix):
                    continue
                rest = rel[len(prefix) :]
            if _rule_matches(rule, rest):
                matched = not rule.negated
        return matched


def resolve_repo_map_settings(
    task_enabled: bool | None,
    task_tokens: int | None,
    *,
    profile_enabled: bool = True,
    profile_tokens: int = REPO_MAP_TOKENS,
) -> tuple[bool, int]:
    """Task front matter wins, then the profile. Default on, 4,000 tokens."""

    enabled = profile_enabled if task_enabled is None else bool(task_enabled)
    tokens = profile_tokens if task_tokens is None else int(task_tokens)
    if tokens <= 0:
        tokens = REPO_MAP_TOKENS
    tokens = max(200, min(int(tokens), REPO_MAP_TOKENS_MAX))
    return enabled, tokens


def build_repo_map(
    project: Path,
    *,
    limit_chars: int = REPO_MAP_CHARS,
    task_text: str = "",
    planned_paths: list[str] | tuple[str, ...] | None = None,
    recent_paths: list[str] | tuple[str, ...] | None = None,
    cache: dict | None = None,
) -> str:
    """Markdown symbol index, or "" when the tree has nothing to outline."""

    return render_repo_map(
        project,
        limit_chars=limit_chars,
        task_text=task_text,
        planned_paths=planned_paths,
        recent_paths=recent_paths,
        cache=cache,
    ).text


def render_repo_map(
    project: Path,
    *,
    limit_chars: int = REPO_MAP_CHARS,
    task_text: str = "",
    planned_paths: list[str] | tuple[str, ...] | None = None,
    recent_paths: list[str] | tuple[str, ...] | None = None,
    cache: dict | None = None,
) -> RepoMapResult:
    project = Path(project).resolve()
    if not project.is_dir():
        return RepoMapResult("", 0, 0, False)
    files = iter_project_files(project, suffixes=SOURCE_SUFFIXES, limit=_MAX_SCAN)
    if not files:
        return RepoMapResult("", 0, 0, False)
    store = cache if cache is not None else {}
    outlines = [_outline_cached(project, path, store) for path in files]
    recent = _recent_set(project, recent_paths)
    ranked = _rank(outlines, task_text=task_text, planned_paths=planned_paths, recent=recent)
    return _fit(ranked, limit_chars=max(1, int(limit_chars)), scanned=len(files))


def format_file_list(
    project: Path,
    directory: str = ".",
    *,
    limit: int = 300,
    char_limit: int = 8_000,
) -> str:
    """Project-relative paths under `directory`, skipping build and ignored dirs."""

    project = Path(project).resolve()
    raw = (directory or ".").replace("\\", "/").strip() or "."
    if raw in {".", "./"}:
        start = project
        label = "."
    else:
        raw = raw.lstrip("./")
        start = (project / raw).resolve()
        try:
            start.relative_to(project)
        except ValueError:
            return f"path escapes the project: {directory}"
        label = raw
    if not start.exists():
        return f"path not found: {directory}"
    if start.is_file():
        rel = start.relative_to(project).as_posix()
        return f"{rel} is a file. Use read to open it."
    files = iter_project_files(project, start=start, suffixes=None, limit=max(limit, 1) + 1)
    rels = [path.relative_to(project).as_posix() for path in files]
    rels.sort()
    omitted = 0
    if len(rels) > limit:
        omitted = len(rels) - limit
        rels = rels[:limit]
    lines = list(rels)
    if omitted:
        lines.append(f"[{omitted} more files not listed]")
    if not lines:
        lines.append(f"(no files under {label})")
    lines.append("[skipped bin, obj, .git, build output, vendored dirs, and ignored paths]")
    text = "\n".join(lines) + "\n"
    if len(text) <= char_limit:
        return text
    note = "\n[list truncated]\n"
    return text[: max(0, char_limit - len(note))].rstrip() + note


def iter_project_files(
    project: Path,
    *,
    start: Path | None = None,
    suffixes: set[str] | None = None,
    limit: int = _MAX_SCAN,
) -> list[Path]:
    """Files under `start`, pruned by skip-dir names and .gitignore."""

    project = Path(project).resolve()
    origin = project if start is None else Path(start).resolve()
    try:
        origin.relative_to(project)
    except ValueError:
        return []
    if not origin.is_dir():
        return []
    rules = _load_ignore_rules(project, origin)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(origin):
        current = Path(dirpath)
        rel_dir = _rel_posix(project, current)
        gitignore = current / ".gitignore"
        if gitignore.is_file():
            rules.add_file(gitignore, rel_dir)
        kept: list[str] = []
        for name in dirnames:
            if _skip_dir_name(name):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rules.ignored(rel, is_dir=True):
                continue
            kept.append(name)
        dirnames[:] = sorted(kept)
        for name in sorted(filenames):
            if name.startswith(".") and name not in {".gitignore", ".gitattributes"}:
                continue
            suffix = Path(name).suffix.lower()
            if suffixes is not None and suffix not in suffixes:
                continue
            if ".min." in name.lower():
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rules.ignored(rel, is_dir=False):
                continue
            if _path_has_skip_dir(rel):
                continue
            found.append(current / name)
            if len(found) >= limit:
                return found
    return found


def _load_ignore_rules(project: Path, start: Path) -> _IgnoreRules:
    rules = _IgnoreRules()
    current = project
    rules_path = current / ".gitignore"
    if rules_path.is_file():
        rules.add_file(rules_path, "")
    try:
        rel = start.resolve().relative_to(project)
    except ValueError:
        return rules
    for part in rel.parts:
        current = current / part
        nested = current / ".gitignore"
        if nested.is_file():
            rules.add_file(nested, _rel_posix(project, current))
    return rules


def _rel_posix(project: Path, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(project).as_posix()
    except ValueError:
        return ""
    return "" if rel == "." else rel


def _skip_dir_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in SKIP_DIR_NAMES:
        return True
    return name.startswith(".")


def _path_has_skip_dir(rel: str) -> bool:
    return any(part.lower() in SKIP_DIR_NAMES or part.startswith(".") for part in rel.split("/"))


def _compile_gitignore(pattern: str) -> re.Pattern[str] | None:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**", index):
                if pattern.startswith("**/", index):
                    parts.append("(?:.*/)?")
                    index += 3
                    continue
                parts.append(".*")
                index += 2
                continue
            parts.append("[^/]*")
            index += 1
            continue
        if char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
        index += 1
    try:
        return re.compile("".join(parts))
    except re.error:
        return None


def _rule_matches(rule: _IgnoreRule, rel: str) -> bool:
    if rule.regex.fullmatch(rel):
        return True
    if rule.anchored:
        return False
    return rule.regex.fullmatch(rel.rsplit("/", 1)[-1]) is not None


def _outline_cached(project: Path, path: Path, cache: dict) -> _Outline:
    rel = path.relative_to(project).as_posix()
    try:
        stat = path.stat()
        key = (rel, getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)), stat.st_size)
        mtime = stat.st_mtime
    except OSError:
        key = (rel, 0, 0)
        mtime = 0.0
    cached = cache.get(key)
    if isinstance(cached, _Outline):
        return cached
    outline = _outline_file(path, rel, mtime)
    cache[key] = outline
    return outline


def _outline_file(path: Path, rel: str, mtime: float) -> _Outline:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return _Outline(rel, f"{rel} (unreadable: {exc.__class__.__name__})", rel, mtime=mtime)
    if size > _MAX_READ:
        return _Outline(rel, f"{rel} (large file, not outlined)", rel, mtime=mtime)
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    except OSError as exc:
        return _Outline(rel, f"{rel} (unreadable: {exc.__class__.__name__})", rel, mtime=mtime)
    if "\0" in text[:8192]:
        return _Outline(rel, rel, rel, mtime=mtime)
    suffix = path.suffix.lower()
    if suffix == ".py":
        symbols, names, refs = _outline_python(text)
    elif suffix == ".cs" or suffix == ".csx":
        symbols, names, refs = _outline_csharp(text)
    elif suffix in {".csproj", ".fsproj", ".vbproj"}:
        symbols, names, refs = _outline_csproj(text)
    elif suffix == ".sln":
        symbols, names, refs = _outline_sln(text)
    else:
        symbols, names, refs = _outline_generic(text, suffix)
    names.add(path.stem.lower())
    line = rel if not symbols else f"{rel}: " + "; ".join(symbols)
    short = rel if not symbols else f"{rel}: {symbols[0]}"
    return _Outline(rel, line, short, {name.lower() for name in names if name}, {ref.lower() for ref in refs if ref}, mtime)


def _outline_python(source: str) -> tuple[list[str], set[str], set[str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ["(syntax error)"], set(), set()
    symbols: list[str] = []
    names: set[str] = set()
    refs: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_py_signature(node))
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            symbols.append(_py_class(node))
            names.add(node.name)
            for base in node.bases:
                base_name = _py_name(base)
                if base_name:
                    refs.add(base_name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                refs.add(alias.name.split(".")[0])
                refs.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                refs.add(node.module.split(".")[0])
                refs.add(node.module)
            for alias in node.names:
                refs.add(alias.name)
        if len(symbols) >= 12:
            break
    return symbols, names, refs


def _py_class(node: ast.ClassDef) -> str:
    methods: list[str] = []
    nested: list[str] = []
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.name.startswith("_"):
            methods.append(_py_signature(child))
        elif isinstance(child, ast.ClassDef):
            nested.append(child.name)
        if len(methods) >= 6 and len(nested) >= 3:
            break
    text = f"class {node.name}"
    if methods:
        text += " [" + ", ".join(methods[:6]) + "]"
    if nested:
        text += " nested " + ", ".join(nested[:3])
    return text


def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args: list[str] = []
    for arg in list(node.args.posonlyargs) + list(node.args.args):
        if arg.arg in {"self", "cls"}:
            continue
        args.append(arg.arg)
    if len(args) > 6:
        args = args[:6] + ["..."]
    return f"{node.name}({', '.join(args)})"


def _py_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _outline_generic(source: str, suffix: str) -> tuple[list[str], set[str], set[str]]:
    patterns = _GENERIC_FALLBACK.get(suffix, ())
    symbols: list[str] = []
    names: set[str] = set()
    for pattern in patterns:
        for match in pattern.finditer(source):
            name = match.group(1)
            if name in _CS_KEYWORDS or name.startswith("_"):
                continue
            params = ""
            if match.lastindex and match.lastindex >= 2:
                raw = " ".join((match.group(2) or "").split())
                if len(raw) > 60:
                    raw = raw[:57] + "..."
                params = f"({raw})"
            label = f"{name}{params}" if params else name
            if label in symbols:
                continue
            symbols.append(label)
            names.add(name)
            if len(symbols) >= 12:
                return symbols, names, set()
    return symbols, names, set()


def _outline_csproj(source: str) -> tuple[list[str], set[str], set[str]]:
    symbols: list[str] = []
    names: set[str] = set()
    refs: set[str] = set()
    namespace = re.search(r"<RootNamespace>\s*([^<]+?)\s*</RootNamespace>", source)
    if namespace:
        value = namespace.group(1).strip()
        symbols.append(f"RootNamespace {value}")
        names.add(value)
    assembly = re.search(r"<AssemblyName>\s*([^<]+?)\s*</AssemblyName>", source)
    if assembly:
        value = assembly.group(1).strip()
        symbols.append(f"AssemblyName {value}")
        names.add(value)
    for include in re.findall(r"<ProjectReference\s+[^>]*Include=\"([^\"]+)\"", source):
        stem = Path(include.replace("\\", "/")).stem
        symbols.append(f"ref {stem}")
        refs.add(stem)
        if len(symbols) >= 12:
            break
    return symbols, names, refs


def _outline_sln(source: str) -> tuple[list[str], set[str], set[str]]:
    symbols: list[str] = []
    names: set[str] = set()
    for name in re.findall(r'Project\("[^"]+"\)\s*=\s*"([^"]+)"', source):
        symbols.append(f"project {name}")
        names.add(name)
        if len(symbols) >= 12:
            break
    return symbols, names, set()


def _outline_csharp(source: str) -> tuple[list[str], set[str], set[str]]:
    masked = _mask_csharp(source)
    refs: set[str] = set()
    for match in _USING.finditer(source):
        full = match.group(1)
        refs.add(full)
        refs.add(full.rsplit(".", 1)[-1])
    finished: list[tuple[int, str, str, list[str]]] = []
    names: set[str] = set()
    stack: list[dict] = []
    pending: dict | None = None
    file_ns = ""
    depth = 0
    seq = 0

    def close_to(level: int) -> None:
        while stack and level < stack[-1]["body_depth"]:
            frame = stack.pop()
            if frame["kind"] == "type":
                parents = [item["name"] for item in stack if item["kind"] == "type"]
                qual = ".".join([*parents, frame["name"]])
                finished.append((frame["seq"], frame["display"], qual, list(frame["members"])))
                names.add(frame["plain"])
                names.add(qual)
                for ref in frame["bases"]:
                    refs.add(ref)

    for raw_line in masked.splitlines():
        code = raw_line.strip()
        opens = "{" in code
        signature = re.sub(r"\{.*", "", code).strip()
        brace_delta = raw_line.count("{") - raw_line.count("}")
        if signature.startswith("#") or signature.startswith("["):
            depth = max(0, depth + brace_delta)
            if pending and depth >= pending["body_depth"]:
                stack.append(pending)
                pending = None
            close_to(depth)
            continue
        file_match = re.fullmatch(r"namespace\s+([A-Za-z_][\w.]*)\s*;", signature)
        if file_match:
            file_ns = file_match.group(1)
            names.add(file_ns)
            names.add(file_ns.rsplit(".", 1)[-1])
            depth = max(0, depth + brace_delta)
            close_to(depth)
            continue
        block_ns = re.fullmatch(r"namespace\s+([A-Za-z_][\w.]*)", signature)
        declared = False
        if block_ns and _accepts_type(depth, stack):
            name = block_ns.group(1)
            names.add(name)
            pending = {
                "kind": "ns",
                "name": name,
                "display": "namespace",
                "plain": name.rsplit(".", 1)[-1],
                "body_depth": depth + 1,
                "members": [],
                "bases": [],
            }
            if opens:
                stack.append(pending)
                pending = None
            declared = True
        if not declared:
            parsed = _csharp_type(signature) if _accepts_type(depth, stack) else None
            if parsed:
                display, plain, bases = parsed
                for base in bases:
                    refs.add(base)
                pending = {
                    "kind": "type",
                    "name": plain,
                    "display": display,
                    "plain": re.sub(r"<.*", "", plain),
                    "body_depth": depth + 1,
                    "members": [],
                    "bases": bases,
                    "seq": seq,
                }
                seq += 1
                names.add(pending["plain"])
                if opens:
                    stack.append(pending)
                    pending = None
                declared = True
        if (
            not declared
            and stack
            and stack[-1]["kind"] == "type"
            and depth == stack[-1]["body_depth"]
        ):
            member = _csharp_member(signature)
            if member and len(stack[-1]["members"]) < 12:
                stack[-1]["members"].append(member)
                head = member.split("(", 1)[0].split()[-1]
                if head:
                    names.add(re.sub(r"<.*", "", head))
                for token in re.findall(r"[A-Za-z_]\w*", member):
                    if token[:1].isupper() and token not in _CS_KEYWORDS and len(token) > 2:
                        refs.add(token)
        depth = max(0, depth + brace_delta)
        if pending and depth >= pending["body_depth"]:
            stack.append(pending)
            pending = None
        close_to(depth)
    close_to(0)
    namespace = file_ns
    if not namespace:
        # The last closed block namespace is the one types were in. Prefer a
        # namespace still on the stack, then the first finished type's file.
        for frame in stack:
            if frame["kind"] == "ns":
                namespace = frame["name"]
                break
    if not namespace:
        for line in masked.splitlines():
            match = re.match(r"\s*namespace\s+([A-Za-z_][\w.]*)", line)
            if match and not line.strip().endswith(";"):
                namespace = match.group(1)
                break
    symbols: list[str] = []
    if namespace:
        names.add(namespace)
        names.add(namespace.rsplit(".", 1)[-1])
    prefix = f"namespace {namespace}; " if namespace else ""
    chunks: list[str] = []
    finished.sort(key=lambda item: item[0])
    for _order, display, qual, members in finished[:8]:
        bit = f"{display} {qual}"
        if members:
            bit += " [" + "; ".join(members[:12]) + "]"
        chunks.append(bit)
    if chunks:
        symbols.append(prefix + "; ".join(chunks) if prefix else "; ".join(chunks))
    elif namespace:
        symbols.append(f"namespace {namespace}")
    return symbols, names, refs


def _accepts_type(depth: int, stack: list[dict]) -> bool:
    if not stack:
        return depth == 0
    top = stack[-1]
    return depth == top["body_depth"]


def _csharp_type(signature: str) -> tuple[str, str, list[str]] | None:
    text = signature.strip()
    modifiers: list[str] = []
    while True:
        match = re.match(r"^(?:protected\s+internal|private\s+protected|[A-Za-z]+)\s+", text)
        if not match:
            break
        word = match.group(0).strip()
        first = word.split()[0]
        if first not in _CS_MODIFIERS and word not in {"protected internal", "private protected"}:
            break
        modifiers.append(word)
        text = text[match.end() :]
    kind_match = re.match(r"(record(?:\s+class|\s+struct)?|class|struct|interface|enum)\s+", text)
    if not kind_match:
        return None
    kind = " ".join(kind_match.group(1).split())
    if kind.split()[0] not in _TYPE_KINDS:
        return None
    text = text[kind_match.end() :]
    name_match = re.match(r"([A-Za-z_]\w*)", text)
    if not name_match:
        return None
    name = name_match.group(1)
    text = text[name_match.end() :]
    generic = ""
    if text.startswith("<"):
        generic, text = _read_generic(text)
    plain = name + generic
    partial = any(item == "partial" for item in modifiers)
    display = f"{'partial ' if partial else ''}{kind}"
    bases: list[str] = []
    base_match = re.match(r"\s*:\s*([^{]+)", text)
    if base_match:
        for part in base_match.group(1).split(","):
            piece = part.strip()
            piece = re.sub(r"\s+where\b.*", "", piece).strip()
            ident = re.match(r"([A-Za-z_][\w.]*)", piece)
            if ident:
                bases.append(ident.group(1).rsplit(".", 1)[-1])
    return display, plain, bases


def _csharp_member(signature: str) -> str | None:
    text = signature.strip().rstrip(";").strip()
    if not text or text.startswith("//"):
        return None
    modifiers: list[str] = []
    while True:
        match = re.match(r"^(?:protected\s+internal|private\s+protected|[A-Za-z]+)\s+", text)
        if not match:
            break
        word = match.group(0).strip()
        first = word.split()[0]
        if first not in _CS_MODIFIERS and word not in {"protected internal", "private protected"}:
            break
        modifiers.append(word)
        text = text[match.end() :]
    if not text:
        return None
    if "(" in text:
        return _csharp_method(text)
    return _csharp_field(text)


def _csharp_method(text: str) -> str | None:
    paren = text.find("(")
    if paren < 0:
        return None
    before = text[:paren].rstrip()
    params, _rest = _read_params(text[paren:])
    name, generic, ret = _split_name(before)
    if not name or name in _CS_KEYWORDS:
        return None
    if name in {"get", "set", "init"}:
        return None
    rendered = _short(params, 70)
    if ret:
        return _short(f"{ret} {name}{generic}({rendered})", 120)
    return _short(f"{name}{generic}({rendered})", 120)


def _csharp_field(text: str) -> str | None:
    text = re.split(r"\s*=", text, maxsplit=1)[0].strip()
    text = re.sub(r"\s*\{.*", "", text).strip()
    if not text or "(" in text:
        return None
    name, _generic, type_name = _split_name(text)
    if not name or name in _CS_KEYWORDS or not type_name:
        return None
    if type_name in _CS_KEYWORDS:
        return None
    return _short(f"{type_name} {name}", 80)


def _split_name(before: str) -> tuple[str, str, str]:
    """Return name, generic suffix, and the type text in front of the name."""

    text = before.strip()
    generic = ""
    if text.endswith(">"):
        start = _generic_start(text)
        if start >= 0:
            generic = text[start:]
            text = text[:start].rstrip()
    match = re.search(r"([A-Za-z_]\w*)$", text)
    if not match:
        return "", generic, ""
    name = match.group(1)
    ret = text[: match.start()].strip()
    ret = re.sub(r"\s+", " ", ret)
    return name, generic, ret


def _generic_start(text: str) -> int:
    depth = 0
    for index in range(len(text) - 1, -1, -1):
        char = text[index]
        if char == ">":
            depth += 1
        elif char == "<":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _read_generic(text: str) -> tuple[str, str]:
    if not text.startswith("<"):
        return "", text
    depth = 0
    for index, char in enumerate(text):
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
            if depth == 0:
                return text[: index + 1], text[index + 1 :]
    return text, ""


def _read_params(text: str) -> tuple[str, str]:
    if not text.startswith("("):
        return "", text
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                inner = " ".join(text[1:index].split())
                return inner, text[index + 1 :]
    return " ".join(text[1:].split()), ""


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _mask_csharp(source: str) -> str:
    """Replace comments and string contents with spaces. Braces in code stay."""

    chars = list(source)
    index = 0
    length = len(chars)
    mode = "code"
    interp: list[int] = []
    while index < length:
        char = chars[index]
        nxt = chars[index + 1] if index + 1 < length else ""
        if mode == "line":
            if char == "\n":
                mode = "code"
            else:
                chars[index] = " "
            index += 1
            continue
        if mode == "block":
            if char == "*" and nxt == "/":
                chars[index] = " "
                chars[index + 1] = " "
                index += 2
                mode = "code"
                continue
            if char != "\n":
                chars[index] = " "
            index += 1
            continue
        if mode in {"string", "verbatim", "interp"}:
            if mode == "verbatim":
                if char == '"' and nxt == '"':
                    chars[index] = " "
                    chars[index + 1] = " "
                    index += 2
                    continue
                if char == '"':
                    chars[index] = " "
                    index += 1
                    mode = "code"
                    continue
                if char != "\n":
                    chars[index] = " "
                index += 1
                continue
            if char == "\\" and mode == "string" and nxt:
                chars[index] = " "
                chars[index + 1] = " "
                index += 2
                continue
            if mode == "interp" and char == "{":
                if nxt == "{":
                    chars[index] = " "
                    chars[index + 1] = " "
                    index += 2
                    continue
                chars[index] = " "
                interp.append(0)
                mode = "code"
                index += 1
                continue
            if mode == "interp" and char == "}" and nxt == "}":
                chars[index] = " "
                chars[index + 1] = " "
                index += 2
                continue
            if char == '"':
                chars[index] = " "
                index += 1
                mode = "code"
                continue
            if char != "\n":
                chars[index] = " "
            index += 1
            continue
        # code
        if interp and char == "{":
            interp[-1] += 1
        elif interp and char == "}":
            if interp[-1] == 0:
                chars[index] = " "
                interp.pop()
                mode = "interp"
                index += 1
                continue
            interp[-1] -= 1
        if char == "/" and nxt == "/":
            chars[index] = " "
            chars[index + 1] = " "
            index += 2
            mode = "line"
            continue
        if char == "/" and nxt == "*":
            chars[index] = " "
            chars[index + 1] = " "
            index += 2
            mode = "block"
            continue
        if char == "@" and nxt == '"':
            chars[index] = " "
            chars[index + 1] = " "
            index += 2
            mode = "verbatim"
            continue
        if char == "$" and nxt == "@" and index + 2 < length and chars[index + 2] == '"':
            chars[index] = chars[index + 1] = chars[index + 2] = " "
            index += 3
            mode = "interp"
            continue
        if char == "@" and nxt == "$" and index + 2 < length and chars[index + 2] == '"':
            chars[index] = chars[index + 1] = chars[index + 2] = " "
            index += 3
            mode = "interp"
            continue
        if char == "$" and nxt == '"':
            chars[index] = " "
            chars[index + 1] = " "
            index += 2
            mode = "interp"
            continue
        if char == '"':
            # C# 11 raw string literals.
            if source.startswith('"""', index):
                end = source.find('"""', index + 3)
                stop = length if end < 0 else end + 3
                while index < stop:
                    if chars[index] != "\n":
                        chars[index] = " "
                    index += 1
                continue
            chars[index] = " "
            index += 1
            mode = "string"
            continue
        if char == "'" and nxt and index + 2 < length:
            chars[index] = " "
            index += 1
            if chars[index] == "\\" and index + 1 < length:
                chars[index] = " "
                chars[index + 1] = " "
                index += 2
            else:
                chars[index] = " "
                index += 1
            if index < length and chars[index] == "'":
                chars[index] = " "
                index += 1
            continue
        index += 1
    return "".join(chars)


def _recent_set(project: Path, recent_paths: list[str] | tuple[str, ...] | None) -> set[str]:
    if recent_paths is not None:
        return {_norm_rel(path) for path in recent_paths if _norm_rel(path)}
    discovered = _git_recent(project)
    return {_norm_rel(path) for path in discovered}


def _git_recent(project: Path, limit: int = 30) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "log", "-n", "15", "--name-only", "--pretty=format:"],
            cwd=project,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    found: list[str] = []
    for line in result.stdout.splitlines():
        rel = line.strip().replace("\\", "/")
        if rel and rel not in found:
            found.append(rel)
        if len(found) >= limit:
            break
    return found


def _norm_rel(path: str) -> str:
    rel = str(path).replace("\\", "/").strip().lstrip("./")
    return rel.lower()


def _task_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in re.finditer(r"`([^`]{2,80})`", text or ""):
        token = match.group(1).strip().lower()
        if token:
            tokens.add(token)
    for match in _TASK_WORD.finditer(text or ""):
        word = match.group(0)
        low = word.replace("\\", "/").lower()
        if low in _TASK_STOP:
            continue
        camel = any(char.isupper() for char in word[1:])
        if camel or "_" in word or "/" in low or "." in low or len(word) >= 8:
            tokens.add(low)
        if camel:
            for part in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", word):
                if len(part) >= 6 and part.lower() not in _TASK_STOP:
                    tokens.add(part.lower())
    return tokens


def _rank(
    outlines: list[_Outline],
    *,
    task_text: str,
    planned_paths: list[str] | tuple[str, ...] | None,
    recent: set[str],
) -> list[_Outline]:
    task_low = (task_text or "").lower()
    tokens = _task_tokens(task_text)
    planned = {_norm_rel(path) for path in (planned_paths or []) if _norm_rel(path)}
    scores = {id(item): _score_outline(item, task_low, tokens, planned, recent) for item in outlines}
    strong = [item for item in outlines if scores[id(item)] >= 50]
    strong_names: set[str] = set()
    strong_refs: set[str] = set()
    for item in strong:
        strong_names |= item.names
        strong_names.add(Path(item.rel).stem.lower())
        strong_refs |= item.refs
    for item in outlines:
        if scores[id(item)] >= 50:
            continue
        if (item.refs & strong_names) or (item.names & strong_refs):
            scores[id(item)] += 25
    if not recent:
        newest = sorted(outlines, key=lambda item: item.mtime, reverse=True)[:8]
        for item in newest:
            scores[id(item)] += 10
    return sorted(outlines, key=lambda item: (-scores[id(item)], item.rel.lower()))


def _score_outline(
    item: _Outline,
    task_low: str,
    tokens: set[str],
    planned: set[str],
    recent: set[str],
) -> int:
    score = 0
    rel = item.rel.lower()
    stem = Path(item.rel).stem.lower()
    if rel and rel in task_low:
        score += 100
    elif stem and len(stem) >= 3 and stem in tokens:
        score += 80
    for part in rel.split("/"):
        if len(part) >= 4 and part in task_low:
            score += 30
            break
    for name in item.names:
        if len(name) >= 4 and name in tokens:
            score += 50
            break
    if rel in planned or any(rel.endswith("/" + path) or path.endswith("/" + rel) for path in planned):
        score += 80
    if rel in recent:
        score += 30
    return score


def _fit(ranked: list[_Outline], *, limit_chars: int, scanned: int) -> RepoMapResult:
    header = f"## Repo map\n{MAP_MARKER}\n"
    if limit_chars < len(header) + 8:
        note = "- [repo map truncated]\n"
        text = (header + note)[:limit_chars]
        return RepoMapResult(text if text.endswith("\n") else text + "\n", scanned, 0, True)
    body: list[str] = []
    used = len(header) + 1
    included = 0
    truncated = False
    note_room = 48
    for item in ranked:
        line = "- " + item.line
        if len(line) > _LINE_CAP:
            line = line[: _LINE_CAP - 4].rstrip() + " ..."
            truncated = True
        if used + len(line) + 1 + note_room > limit_chars:
            short = "- " + item.short
            if len(short) > 180:
                short = short[:176].rstrip() + " ..."
            if used + len(short) + 1 + note_room <= limit_chars:
                body.append(short)
                used += len(short) + 1
                included += 1
                truncated = True
            break
        body.append(line)
        used += len(line) + 1
        included += 1
    omitted = len(ranked) - included
    if omitted > 0:
        truncated = True
        note = f"- [{omitted} more files omitted]"
        if used + len(note) + 1 <= limit_chars:
            body.append(note)
        else:
            body.append("- [repo map truncated]")
    if not body:
        return RepoMapResult("", scanned, 0, False)
    text = header + "\n".join(body) + "\n"
    if len(text) > limit_chars:
        note = "\n- [repo map truncated]\n"
        text = text[: max(0, limit_chars - len(note))].rstrip() + note
        truncated = True
    return RepoMapResult(text, scanned, included, truncated)
