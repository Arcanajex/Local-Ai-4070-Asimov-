"""Repo map outlining, ranking, budget, and project-context truncation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from local_agent.client import ChatResult, ToolCall
from local_agent.config import get_profile
from local_agent.context import compact_messages
from local_agent.overnight import apply_project_defaults, load_task, parse_task, run_overnight
from local_agent.project_context import attach_repo_map, pack_project_context
from local_agent.repo_map import (
    REPO_MAP_TOKENS,
    build_repo_map,
    render_repo_map,
    resolve_repo_map_settings,
)
from local_agent.shells import command_succeeds

FILE_SCOPED = """
namespace Esoteric.Editor;

public partial class Viewport<T> where T : class
{
    private readonly Camera _camera;
    public int Width { get; set; }

    public void Draw(T item)
    {
        var label = "class Fake { void Nope() { } }";
        if (item == null) { return; }
    }

    public partial class Overlay
    {
        public void Paint()
        {
        }
    }
}
"""

BLOCK_NS = """
namespace Esoteric.Core
{
    public partial class Viewport<T>
    {
        public void Resize(int width, int height)
        {
        }
    }
}
"""

CAMERA = """
namespace Esoteric.Editor;

public class CameraRig
{
    private readonly ViewportWindow _view;
    public void Aim() {}
}
"""


def test_csharp_outline_covers_generics_nested_partial_and_file_scoped_namespace(tmp_path):
    editor = tmp_path / "src" / "Esoteric.Editor"
    editor.mkdir(parents=True)
    (editor / "Viewport.cs").write_text(FILE_SCOPED, encoding="utf-8")
    (editor / "Viewport.Resize.cs").write_text(BLOCK_NS, encoding="utf-8")
    mapped = build_repo_map(tmp_path, limit_chars=8000)
    assert "src/Esoteric.Editor/Viewport.cs" in mapped
    assert "namespace Esoteric.Editor" in mapped
    assert "partial class Viewport<T>" in mapped
    assert "void Draw(T item)" in mapped
    assert "int Width" in mapped
    assert "Camera _camera" in mapped
    assert "partial class Viewport<T>.Overlay" in mapped
    assert "void Paint()" in mapped
    assert "namespace Esoteric.Core" in mapped
    assert "void Resize(int width, int height)" in mapped
    assert "Fake" not in mapped
    assert "Nope" not in mapped


def test_repo_map_skips_bin_obj_and_gitignore(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.cs").write_text("namespace AppSpace;\npublic class App { public void Run() {} }\n", encoding="utf-8")
    (src / "obj").mkdir()
    (src / "obj" / "Debug.cs").write_text("class ShouldNotAppear {}\n", encoding="utf-8")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "Out.cs").write_text("class BinType {}\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("hidden/\n*.generated.cs\n", encoding="utf-8")
    (tmp_path / "hidden").mkdir()
    (tmp_path / "hidden" / "Secret.cs").write_text("class Secret {}\n", encoding="utf-8")
    (src / "Noise.generated.cs").write_text("class Noise {}\n", encoding="utf-8")
    mapped = build_repo_map(tmp_path, limit_chars=8000)
    assert "src/App.cs" in mapped
    assert "class App" in mapped
    assert "ShouldNotAppear" not in mapped
    assert "BinType" not in mapped
    assert "Secret" not in mapped
    assert "Noise" not in mapped


def test_repo_map_ranks_task_plan_recent_and_neighbours(tmp_path):
    methods = "\n".join(
        f"    public void Work{n}(int left, int right, string name) {{}}" for n in range(8)
    )
    for index in range(12):
        path = tmp_path / f"Filler{index}.cs"
        path.write_text(f"public class Filler{index}\n{{\n{methods}\n}}\n", encoding="utf-8")
    editor = tmp_path / "Editor"
    editor.mkdir()
    (editor / "Viewport.cs").write_text(
        "namespace Esoteric.Editor;\npublic class ViewportWindow { public void Draw() {} }\n",
        encoding="utf-8",
    )
    other = tmp_path / "Other"
    other.mkdir()
    (other / "Camera.cs").write_text(CAMERA.replace("namespace Esoteric.Editor;", "namespace Other;"), encoding="utf-8")
    (editor / "Recent.cs").write_text(
        "namespace Esoteric.Editor;\npublic class RecentTool { public void Touch() {} }\n",
        encoding="utf-8",
    )
    mapped = build_repo_map(
        tmp_path,
        limit_chars=430,
        task_text="Update ViewportWindow in Editor/Viewport.cs so the view draws.",
        planned_paths=["Editor/Recent.cs"],
        recent_paths=["Editor/Recent.cs"],
    )
    assert "ViewportWindow" in mapped
    assert "CameraRig" in mapped
    assert "RecentTool" in mapped
    assert "Filler" not in mapped
    assert mapped.find("Viewport") < mapped.find("Camera")
    assert "more files omitted" in mapped


def test_repo_map_fits_the_character_budget(tmp_path):
    for index in range(40):
        (tmp_path / f"File{index}.cs").write_text(
            f"namespace N{index};\npublic class Type{index} {{ public void Run(int value) {{}} public int Count; }}\n",
            encoding="utf-8",
        )
    result = render_repo_map(tmp_path, limit_chars=1200, task_text="Type3 is the one to edit")
    assert len(result.text) <= 1200
    assert result.truncated
    assert "Type3" in result.text
    assert "more files omitted" in result.text or "repo map truncated" in result.text
    assert result.text.count("## Repo map") == 1


def test_python_and_generic_fallback(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "calc.py").write_text(
        "class Box:\n    def area(self):\n        return 1\n\ndef total():\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "web.js").write_text("export function greet(name) {\n  return name;\n}\nclass Panel {}\n", encoding="utf-8")
    mapped = build_repo_map(tmp_path, limit_chars=4000)
    assert "pkg/calc.py" in mapped
    assert "class Box" in mapped
    assert "total()" in mapped
    assert "greet(name)" in mapped
    assert "Panel" in mapped


def test_repo_map_settings_follow_task_then_agents_then_profile(tmp_path):
    assert resolve_repo_map_settings(None, None) == (True, REPO_MAP_TOKENS)
    assert resolve_repo_map_settings(False, 1500) == (False, 1500)
    assert resolve_repo_map_settings(None, 9000) == (True, 6000)
    task = parse_task("---\nrepo_map: false\nrepo_map_tokens: 1200\n---\n# T\n\n## Goal\n\nX\n")
    assert task.repo_map is False
    assert task.repo_map_tokens == 1200
    (tmp_path / "AGENTS.md").write_text("---\nrepo_map: false\nrepo_map_tokens: 2500\n---\n# Rules\n", encoding="utf-8")
    unset = parse_task("# T\n\n## Goal\n\nX\n")
    apply_project_defaults(unset, tmp_path)
    assert unset.repo_map is False
    assert unset.repo_map_tokens == 2500
    explicit = parse_task("---\nrepo_map: true\nrepo_map_tokens: 1800\n---\n# T\n")
    apply_project_defaults(explicit, tmp_path)
    assert explicit.repo_map is True
    assert explicit.repo_map_tokens == 1800


def test_attach_repo_map_replaces_instead_of_duplicating():
    project = "Project context\n\n## AGENTS.md\n\nKeep the rules.\n"
    first = attach_repo_map(
        project,
        "## Repo map\n<!-- asimov-repo-map -->\n- a.cs: class A\n",
        context_tokens=8192,
        generation_tokens=512,
    )
    second = attach_repo_map(
        first,
        "## Repo map\n<!-- asimov-repo-map -->\n- b.cs: class B\n",
        context_tokens=8192,
        generation_tokens=512,
    )
    assert second.count("## Repo map") == 1
    assert "class B" in second
    assert "class A" not in second
    assert "Keep the rules." in second
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": second + "\nTASK build it"},
    ]
    for index in range(8):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "shell", "arguments": {"command": str(index)}}}],
            }
        )
        messages.append({"role": "tool", "content": "x" * 4000})
    compacted = compact_messages(messages, 900)
    joined = "\n".join(str(message.get("content") or "") for message in compacted)
    assert joined.count("## Repo map") == 1
    assert "class B" in joined


def test_agents_truncation_keeps_relevant_sections_and_reports_omissions(tmp_path):
    filler = "\n".join(f"## Section {index}\n" + ("word " * 40) for index in range(25))
    text = (
        "# Guide\n\nIntro stays.\n\n"
        + filler
        + "\n## ImGui cheat sheet\n\nKEEP_THIS_CHEAT\n\n"
        + "## ViewportWindow\n\nDraw the viewport.\n"
    )
    (tmp_path / "AGENTS.md").write_text(text, encoding="utf-8")
    pack = pack_project_context(
        tmp_path,
        [],
        limit=2200,
        focus="Change ViewportWindow.Draw so the view updates.",
        markers=["cheat sheet"],
    )
    assert len(pack.text) <= 2200
    assert "KEEP_THIS_CHEAT" in pack.text
    assert "ViewportWindow" in pack.text
    assert "[truncated - read AGENTS.md for the rest]" in pack.text
    assert pack.files[0].truncated
    assert pack.files[0].original_chars > pack.files[0].kept_chars
    assert any(item.startswith("Section") for item in pack.files[0].omitted_headings)
    assert "Omitted sections" in pack.text


def test_phase_context_refreshes_the_map_after_an_edit(tmp_path):
    project = _repo(tmp_path / "repo")
    (project / "src").mkdir()
    (project / "src" / "Viewport.cs").write_text(
        "namespace Esoteric.Editor;\npublic class ViewportWindow { public void Draw() {} }\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/Viewport.cs"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "viewport"],
        cwd=project,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    task_path = tmp_path / "task.md"
    task_path.write_text(
        "---\n"
        f"test_command: {command_succeeds()}\n"
        "---\n"
        "# Viewport\n\n## Goal\n\nUpdate ViewportWindow.\n\n"
        "## Acceptance criteria\n\n- [ ] it works\n\n"
        "## Out of scope\n\n- no extra files\n",
        encoding="utf-8",
    )
    seen: list[str] = []
    wrote = {"n": 0}

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            seen.append(prompt)
            if "Review the diff" in prompt:
                return ChatResult(
                    content=(
                        "Acceptance criteria:\n- it works: met\n"
                        "Out-of-scope files touched:\nnone\n"
                        "Conflicts with AGENTS.md or decisions:\nnone\n"
                        "Verdict: accept\n"
                    ),
                    reasoning="",
                    tool_calls=[],
                    finish_reason="stop",
                )
            if "Implement the task." in prompt and wrote["n"] == 0:
                wrote["n"] += 1
                return ChatResult(
                    content="",
                    reasoning="",
                    tool_calls=[
                        ToolCall(
                            id="w",
                            name="write",
                            arguments={
                                "path": "src/NewSymbol.cs",
                                "content": "namespace Esoteric.Editor;\npublic class NewSymbol { public void Mark() {} }\n",
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return ChatResult(content="Edit Viewport.cs and stop.", reasoning="", tool_calls=[], finish_reason="stop")

    run_overnight(
        task=load_task(task_path),
        project=project,
        profile=get_profile("day"),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=1,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=2,
    )
    plan = next(prompt for prompt in seen if "numbered plan" in prompt)
    review = next(prompt for prompt in seen if "Review the diff" in prompt)
    assert plan.count("## Repo map") == 1
    assert "ViewportWindow" in plan
    assert review.count("## Repo map") == 1
    assert "NewSymbol" in review
    log = (tmp_path / "logs" / "events.log").read_text(encoding="utf-8")
    assert "repo map" in log
    assert "chars" not in log or "repo map" in log


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
