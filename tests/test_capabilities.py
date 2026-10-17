import pytest

from agent_factory.capabilities import (
    CapabilityError,
    ScopeViolation,
    SkillCatalog,
    ToolBlocked,
    ToolContext,
    ToolNotAvailable,
    default_registry,
)


@pytest.fixture
def reg():
    return default_registry()


def ctx(tmp_path, permitted=(), approved=(), events=None):
    sink = events if events is not None else []
    return ToolContext("run_1", tmp_path, set(permitted), set(approved),
                       audit=lambda t, d: sink.append((t, d)))


def test_unregistered_tool_fails_safely(reg, tmp_path):
    with pytest.raises(ToolNotAvailable):
        reg.invoke("python.exec", {"code": "1"}, ctx(tmp_path, ["python.exec"]))


def test_tool_must_be_permitted_by_blueprint(reg, tmp_path):
    events = []
    with pytest.raises(ToolBlocked):
        reg.invoke("text.stats", {"text": "a b"}, ctx(tmp_path, events=events))
    assert events[0][0] == "tool_blocked"


def test_side_effect_tool_requires_approval(reg, tmp_path):
    events = []
    c = ctx(tmp_path, permitted=["files.write_text"], events=events)
    with pytest.raises(ToolBlocked):
        reg.invoke("files.write_text", {"path": "out.txt", "content": "x"}, c)
    assert events == [("tool_blocked", {"tool": "files.write_text",
                                        "reason": "side effect not approved"})]
    c.approved_tools.add("files.write_text")
    out = reg.invoke("files.write_text", {"path": "out/a.txt", "content": "hi"}, c)
    assert out == {"path": "out/a.txt", "bytes": 2}
    assert (tmp_path / "out/a.txt").read_text() == "hi"


def test_filesystem_scope_is_enforced(reg, tmp_path):
    c = ctx(tmp_path, permitted=["files.read_text"])
    with pytest.raises(ScopeViolation):
        reg.invoke("files.read_text", {"path": "../../etc/passwd"}, c)
    with pytest.raises(ScopeViolation):
        reg.invoke("files.read_text", {"path": "/etc/passwd"}, c)


def test_invalid_tool_input_rejected(reg, tmp_path):
    with pytest.raises(CapabilityError):
        reg.invoke("text.stats", {"txt": "x"}, ctx(tmp_path, ["text.stats"]))


def test_numeric_description(reg, tmp_path):
    csv_text = "region,revenue,cost\nnorth,100,60\nsouth,50,20\n"
    out = reg.invoke("data.describe_numeric", {"csv_text": csv_text},
                     ctx(tmp_path, ["data.describe_numeric"]))
    assert out["numeric_columns"] == ["cost", "revenue"]
    assert out["stats"]["revenue"] == {"min": 50.0, "max": 100.0, "mean": 75.0, "sum": 150.0}


def test_write_tools_always_require_approval(reg):
    assert reg.get("files.write_text").requires_approval
    assert not reg.get("text.stats").requires_approval


def test_skill_catalog():
    cat = SkillCatalog()
    assert "reviewer" in cat
    with pytest.raises(CapabilityError):
        cat.get("hacker")
