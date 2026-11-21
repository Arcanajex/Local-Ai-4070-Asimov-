"""NuGet XML docs, assembly fallback, compiler errors, and fix-prompt injection."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

from local_agent.api_index import (
    autolookup,
    build_api_index,
    compiler_queries,
    discover_packages,
    lookup,
    output_needs_api_lookup,
    parse_doc_xml,
    resolve_api_lookup_settings,
    trim_text,
)
from local_agent.client import ChatResult, ToolCall
from local_agent.config import get_profile, write_override
from local_agent.dotnet_meta import read_public_api
from local_agent.overnight import apply_project_defaults, load_task, parse_task, run_overnight
from local_agent.shells import command_succeeds, python_shell
from local_agent.tools import Workspace, dispatch, tool_schemas

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "api"
XML = (FIXTURES / "Widget.xml").read_text(encoding="utf-8")
ERRORS = "\n".join(
    [
        "Program.cs(10,5): error CS1061: 'Widget' does not contain a definition for 'Draww' "
        "and no accessible extension method 'Draww' accepting a first argument of type 'Widget' could be found",
        "Program.cs(11,1): error CS7036: There is no argument given that corresponds to the "
        "required parameter 'b' of 'Widget.Add(int, int)'",
        "Program.cs(12,1): error CS0117: 'Widget' does not contain a definition for 'BeginChild'",
        "Program.cs(13,8): error CS1503: Argument 2: cannot convert from 'int' to 'System.Numerics.Vector2'",
        "Program.cs(14,1): error CS1501: No overload for method 'Button' takes 3 arguments",
        "Program.cs(15,1): error CS0122: 'Widget.Hidden' is inaccessible due to its protection level",
        "Program.cs(16,7): error CS0246: The type or namespace name 'ImGui' could not be found "
        "(are you missing a using directive or an assembly reference?)",
    ]
)


def test_xml_docs_cover_overloads_nested_types_and_generic_ids():
    records = {item["member_id"]: item for item in parse_doc_xml(XML)}
    draw = records["M:Ns.Widget.Draw(System.Int32)"]
    assert draw["signature"] == "Ns.Widget.Draw(int count)"
    assert "count" in draw["summary"] and "Add" in draw["summary"]
    assert "M:" not in draw["summary"]
    adds = [item for item in records.values() if item["name"] == "Add"]
    assert {item["signature"] for item in adds} == {
        "Ns.Widget.Add(int a, int b)",
        "Ns.Widget.Add(string a, string b)",
    }
    assert "Returns The sum." in records["M:Ns.Widget.Add(System.Int32,System.Int32)"]["summary"]
    generic = records["M:Ns.Generic`1.Method``1(``0,System.Int32)"]
    assert generic["name"] == "Method"
    assert generic["signature"] == "Ns.Generic<T>.Method<T1>(T1 item, int scale)"
    assert records["T:Ns.Widget.Inner"]["signature"] == "Ns.Widget.Inner"
    assert records["M:Ns.Widget.Inner.Ping"]["parent"] == "Ns.Widget.Inner"
    assert records["P:Ns.Widget.Name"]["kind"] == "property"
    assert records["F:Ns.Widget.Id"]["kind"] == "field"
    broken = parse_doc_xml(
        '<doc><members><member name="M:Ns.Widget.Draw(System.Int32)">'
        "<summary>A & B</summary></member></members></doc>"
    )
    assert broken[0]["summary"] == "A & B"
    assert broken[0]["signature"] == "Ns.Widget.Draw(int)"


def test_package_discovery_assets_csproj_and_tfm(tmp_path):
    cache = tmp_path / "packages"
    _write_package(cache, "widget", "9.9.9", XML, tfm="net8.0")
    net6 = cache / "widget" / "9.9.9" / "lib" / "net6.0"
    net6.mkdir(parents=True)
    (net6 / "Widget.xml").write_text(
        '<doc><members><member name="T:Ns.Net6Only"><summary>old</summary></member></members></doc>',
        encoding="utf-8",
    )
    project = tmp_path / "proj"
    _write_assets(project, cache, package="Widget", version="9.9.9", tfm="net8.0")
    assets_path = project / "obj" / "project.assets.json"
    assets = json.loads(assets_path.read_text(encoding="utf-8"))
    # An older TFM is listed first. The project's net8.0 target has to win.
    assets["targets"] = {
        ".NETCoreApp,Version=v6.0": {
            "Widget/9.9.9": {"type": "package", "compile": {"lib/net6.0/Widget.dll": {}}}
        },
        **assets["targets"],
    }
    assets_path.write_text(json.dumps(assets), encoding="utf-8")
    found = discover_packages(project)
    assert [(item.package_id, item.version) for item in found] == [("Widget", "9.9.9")]
    assert found[0].xml_rel.replace("\\", "/").endswith("lib/net8.0/Widget.xml")
    index = build_api_index(project, cache_dir=tmp_path / "cache", nuget_roots=[cache])
    names = {item.name for item in index.members}
    assert "Widget" in names and "Draw" in names
    assert "Net6Only" not in names
    # A second project with only a PackageReference still resolves the cache.
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Widget" Version="9.9.9" />'
        "</ItemGroup></Project>",
        encoding="utf-8",
    )
    (bare / "nuget.config").write_text(
        '<?xml version="1.0"?><configuration><config>'
        f'<add key="globalPackagesFolder" value="{cache.as_posix()}" />'
        "</config></configuration>",
        encoding="utf-8",
    )
    from_project = discover_packages(bare)
    assert from_project[0].package_id == "Widget"
    indexed = build_api_index(bare, cache_dir=tmp_path / "cache2")
    assert any(item.name == "Draw" for item in indexed.members)


def test_lookup_exact_fuzzy_and_budget(tmp_path):
    project, cache = _project_with_xml(tmp_path)
    index = build_api_index(project, cache_dir=tmp_path / "cache", nuget_roots=[cache])
    exact = lookup(index, "M:Ns.Widget.Draw(System.Int32)")
    assert exact.hits == 1
    assert "Ns.Widget.Draw(int count)" in exact.text
    assert "Draw count times" in exact.text
    overloads = lookup(index, "Add")
    assert overloads.hits == 2
    assert "int a, int b" in overloads.text and "string a, string b" in overloads.text
    nested = lookup(index, "Widget.Inner")
    assert "Ping" in nested.text
    missing = lookup(index, "Widget.Draww")
    assert missing.hits >= 1
    assert "No member 'Draww'" in missing.text
    assert "Closest:" in missing.text and "Draw" in missing.text.split("Closest:", 1)[1]
    generic = lookup(index, "Generic.Method")
    assert "Method<T1>" in generic.text
    long = lookup(index, "Widget", budget=180)
    assert len(long.text) <= 180
    assert long.text.endswith("[trimmed]")
    # The line that was cut is not a partial signature line when a newline fit.
    assert "\n" in long.text
    filler = "\n".join(f"line {index} {'x' * 40}" for index in range(30))
    trimmed = trim_text(filler, 200)
    assert len(trimmed) <= 200
    assert trimmed.endswith("[trimmed]")
    kept = trimmed.removesuffix("\n[trimmed]")
    assert filler.startswith(kept)
    assert kept.splitlines()[-1] in filler.splitlines()


def test_cache_is_per_package_version_and_survives_a_missing_file(tmp_path):
    project, cache = _project_with_xml(tmp_path)
    store = tmp_path / "cache"
    first = build_api_index(project, cache_dir=store, nuget_roots=[cache])
    cached = list(store.rglob("*.json"))
    assert len(cached) == 1
    assert "9.9.9" in cached[0].name or "9.9.9" in str(cached[0])
    xml = cache / "widget" / "9.9.9" / "lib" / "net8.0" / "Widget.xml"
    xml.unlink()
    second = build_api_index(project, cache_dir=store, nuget_roots=[cache])
    assert [item.member_id for item in second.members] == [item.member_id for item in first.members]


def test_dll_fallback_reads_public_signatures_and_a_bad_dll_is_logged(tmp_path):
    dll = FIXTURES / "TinyLib.dll"
    built = _tiny_dll_builder()()
    assert dll.read_bytes() == built
    members = read_public_api(dll)
    by_id = {item.member_id: item.signature for item in members}
    assert by_id["M:Demo.Widget.Draw(System.Int32)"] == "void Demo.Widget.Draw(int count)"
    assert by_id["M:Demo.Widget.Add(System.Int32,System.Int32)"] == "int Demo.Widget.Add(int a, int b)"
    assert by_id["M:Demo.Widget.Add(System.Int32)"] == "int Demo.Widget.Add(int a)"
    assert by_id["M:Demo.Widget.#ctor"] == "void Demo.Widget()"
    assert by_id["F:Demo.Widget.Id"] == "int Demo.Widget.Id"
    assert "get;" in by_id["P:Demo.Widget.Name"]
    assert by_id["M:Demo.Widget.Inner.Ping"] == "void Demo.Widget.Inner.Ping()"
    assert all("Hidden" not in item.signature for item in members)
    assert all("get_Name" not in item.name for item in members)

    cache = tmp_path / "packages"
    package = cache / "tinylib" / "1.0.0" / "lib" / "net8.0"
    package.mkdir(parents=True)
    (package / "TinyLib.dll").write_bytes(dll.read_bytes())
    project = tmp_path / "proj"
    _write_assets(project, cache, package="TinyLib", version="1.0.0", tfm="net8.0", assembly="TinyLib")
    index = build_api_index(project, cache_dir=tmp_path / "cache", nuget_roots=[cache])
    assert any("void Demo.Widget.Draw(int count)" in item.signature for item in index.members)
    assert any(item.source == "dll" for item in index.members)
    assert any("no XML docs; read" in warning for warning in index.warnings)
    found = lookup(index, "Demo.Widget.Add")
    assert "int a, int b" in found.text
    assert "(assembly metadata)" in found.text

    bad = tmp_path / "bad"
    bad_cache = tmp_path / "bad-packages"
    folder = bad_cache / "broken" / "1.0.0" / "lib" / "net8.0"
    folder.mkdir(parents=True)
    (folder / "Broken.dll").write_bytes(b"not a pe file")
    _write_assets(bad, bad_cache, package="Broken", version="1.0.0", tfm="net8.0", assembly="Broken")
    broken = build_api_index(bad, cache_dir=tmp_path / "bad-cache", nuget_roots=[bad_cache])
    assert broken.members == []
    assert any("metadata read failed" in warning for warning in broken.warnings)


def test_compiler_errors_name_the_type_or_member():
    assert output_needs_api_lookup(ERRORS)
    assert not output_needs_api_lookup("exit_code: 1\nAssertionError")
    assert compiler_queries(ERRORS) == [
        "Widget.Draww",
        "Widget.Add",
        "Widget.BeginChild",
        "Vector2",
        "Button",
        "Widget.Hidden",
        "ImGui",
    ]


def test_settings_follow_task_then_agents_then_profile(tmp_path):
    profile = get_profile("day")
    assert profile.api_lookup is True
    assert profile.api_lookup_budget == 1600
    project = tmp_path / "proj"
    project.mkdir()
    (project / "AGENTS.md").write_text(
        "---\napi_lookup: false\napi_lookup_budget: 500\n---\n# Agents\n",
        encoding="utf-8",
    )
    task = parse_task("---\n---\n# Task\n")
    apply_project_defaults(task, project)
    from_agents = resolve_api_lookup_settings(
        task.api_lookup,
        task.api_lookup_budget,
        profile_enabled=profile.api_lookup,
        profile_budget=profile.api_lookup_budget,
    )
    assert from_agents.enabled is False
    assert from_agents.budget == 500
    override = parse_task("---\napi_lookup: true\napi_lookup_budget: 50\n---\n# Task\n")
    apply_project_defaults(override, project)
    clamped = resolve_api_lookup_settings(
        override.api_lookup,
        override.api_lookup_budget,
        profile_enabled=False,
        profile_budget=100,
    )
    assert clamped.enabled is True
    assert clamped.budget == 200
    path = tmp_path / "overrides.json"
    write_override("day", {"api_lookup": False, "api_lookup_budget": 900}, path)
    loaded = get_profile("day", override_path=path)
    assert loaded.api_lookup is False
    assert loaded.api_lookup_budget == 900


def test_lookup_tool_returns_a_budgeted_signature(tmp_path):
    project, cache = _project_with_xml(tmp_path)
    workspace = Workspace(project)
    workspace.nuget_roots = [cache]
    workspace.api_cache_dir = tmp_path / "cache"
    workspace.api_lookup_budget = 240
    result = dispatch("lookup_api", {"query": "Widget"}, workspace)
    assert result.ok
    assert result.hits and result.hits > 0
    assert "Draw(int count)" in result.output
    assert len(result.output) <= 240
    workspace.api_lookup_enabled = False
    disabled = dispatch("lookup_api", {"query": "Widget"}, workspace)
    assert not disabled.ok
    assert "disabled" in disabled.output
    names = [schema["function"]["name"] for schema in tool_schemas({"read", "list_files", "lookup_api"})]
    assert names == ["read", "list_files", "lookup_api"]


def test_step_fix_prompt_gets_autolookup_and_events(tmp_path):
    project = _repo(tmp_path / "repo")
    cache = tmp_path / "packages"
    _write_package(cache, "widget", "9.9.9", XML)
    _write_assets(project, cache, package="Widget", version="9.9.9", tfm="net8.0")
    script = project / "fail_api.py"
    script.write_text(
        "import sys\n"
        'print("Program.cs(10,5): error CS1061: \'Widget\' does not contain a definition for \'Draww\'")\n'
        'print("Program.cs(11,1): error CS7036: There is no argument given that corresponds to the '
        'required parameter \'b\' of \'Widget.Add(int, int)\'")\n'
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=project,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    verify = python_shell(str(script))
    prompts: list[str] = []

    class Client:
        def __init__(self):
            self.looked = False

        def chat(self, messages, *, tools, max_tokens):
            del max_tokens
            prompt = _user(messages)
            prompts.append(prompt)
            names = [item["function"]["name"] for item in (tools or [])]
            if "Split the plan into small verified steps" in prompt:
                assert "lookup_api" in names
                return _text(
                    "### Step 1\nGoal: Call Widget.Draw\nFiles: none\nVerify: " + verify + "\n"
                )
            if "Implement this step only" in prompt:
                assert "lookup_api" in names
                if not self.looked:
                    self.looked = True
                    return _call("lookup_api", {"query": "Widget.Draw"})
                return _text("Used the signature.")
            if "Fix this step only" in prompt or "The tests failed" in prompt:
                return _text("Still broken.")
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(
                    "Acceptance criteria:\n- it works: met\n"
                    "Out-of-scope files touched:\nnone\n"
                    "Conflicts with AGENTS.md or decisions:\nnone\n"
                    "Verdict: accept\n"
                )
            return _text("1. Call Widget.Draw")

    result = run_overnight(
        task=_task(
            tmp_path,
            "---\nsteps: true\nmax_steps: 1\nstep_turns: 2\nstep_replans: 0\n"
            f"test_command: {command_succeeds()}\n---\n" + _body("lookup"),
        ),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=1,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=4,
    )
    assert result.status == "step_failed"
    assert any("API lookup" in prompt and "Draw(int count)" in prompt for prompt in prompts)
    assert any("No member 'Draww'" in prompt and "Widget.Add(int a, int b)" in prompt for prompt in prompts)
    log = (tmp_path / "logs" / "events.log").read_text(encoding="utf-8")
    assert "api_lookup query=Widget.Draw hits=" in log
    assert "api_autolookup query=Widget.Draww, Widget.Add hits=" in log
    assert "api_index" in log and "no XML docs" not in log


def _project_with_xml(tmp_path: Path) -> tuple[Path, Path]:
    cache = tmp_path / "packages"
    _write_package(cache, "widget", "9.9.9", XML)
    project = tmp_path / "proj"
    _write_assets(project, cache, package="Widget", version="9.9.9", tfm="net8.0")
    return project, cache


def _write_package(cache: Path, package: str, version: str, xml: str, *, tfm: str = "net8.0") -> None:
    folder = cache / package / version / "lib" / tfm
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Widget.xml").write_text(xml, encoding="utf-8")


def _write_assets(
    project: Path,
    cache: Path,
    *,
    package: str,
    version: str,
    tfm: str,
    assembly: str = "Widget",
) -> None:
    obj = project / "obj"
    obj.mkdir(parents=True, exist_ok=True)
    dll = f"lib/{tfm}/{assembly}.dll"
    xml = f"lib/{tfm}/{assembly}.xml"
    payload = {
        "version": 3,
        "targets": {
            f".NETCoreApp,Version=v{tfm.removeprefix('net')}": {
                f"{package}/{version}": {
                    "type": "package",
                    "compile": {dll: {}},
                }
            }
        },
        "libraries": {
            f"{package}/{version}": {
                "type": "package",
                "path": f"{package.lower()}/{version}",
                "files": [dll, xml],
            }
        },
        "project": {"frameworks": {tfm: {}}},
        "packageFolders": {str(cache): {}},
    }
    (obj / "project.assets.json").write_text(json.dumps(payload), encoding="utf-8")


def _tiny_dll_builder():
    path = FIXTURES / "build_tiny_dll.py"
    spec = importlib.util.spec_from_file_location("build_tiny_dll", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module.build


def _task(tmp_path: Path, text: str):
    path = tmp_path / "task.md"
    path.write_text(text, encoding="utf-8")
    return load_task(path)


def _body(name: str) -> str:
    return (
        f"# {name}\n\n## Goal\n\nShip {name}.\n\n"
        "## Acceptance criteria\n\n- [ ] it works\n\n"
        "## Out of scope\n\n- no extra files\n"
    )


def _day():
    profile = get_profile("day")
    profile.context = 8192
    profile.agent_max_tokens = 256
    profile.agent_max_tokens_ceiling = 512
    return profile


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return path


def _text(content: str) -> ChatResult:
    return ChatResult(content=content, reasoning="", tool_calls=[], finish_reason="stop")


def _call(name: str, arguments: dict) -> ChatResult:
    return ChatResult(
        content="",
        reasoning="",
        tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


def _user(messages) -> str:
    for message in messages:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""
