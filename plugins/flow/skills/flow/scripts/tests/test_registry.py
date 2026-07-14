from pathlib import Path

import pytest

from _registry import load_registry, registry_by_name

REAL_REGISTRY = Path(__file__).resolve().parent.parent.parent / "stage-registry.toml"

_REFERENCE_DOC_ENTRIES = [e for e in load_registry(REAL_REGISTRY) if e.reference_doc is not None]


@pytest.mark.parametrize(
    "entry",
    _REFERENCE_DOC_ENTRIES,
    ids=[e.name for e in _REFERENCE_DOC_ENTRIES],
)
def test_reference_doc_resolves(entry):
    assert (REAL_REGISTRY.parent / entry.reference_doc).is_file()


def test_load_real_registry():
    entries = load_registry(REAL_REGISTRY)
    names = [e.name for e in entries]
    assert "ticket" in names
    assert "commit" in names
    assert "review_brief" in names
    assert "reflect" in names


def test_registry_by_name_fields():
    by = registry_by_name(REAL_REGISTRY)
    assert by["commit"].required_fields == ["commit_type", "commit_summary"]
    assert "records_diff_baseline" in by["implement"].roles
    assert "agent_routed" in by["implement"].roles
    assert "agent_routed" in by["e2e"].roles
    assert by["implement"].default_timeout_min == 30
    assert by["review_brief"].default_handler == "inline"
    assert by["review_brief"].required_predecessors == ["create_pr"]
    assert "agent_routed" not in by["review_brief"].roles
    assert "agent_routed" not in by["reflect"].roles
    assert by["review_brief"].default_handler == "inline"
    assert by["reflect"].default_handler == "inline"


def test_load_malformed_non_array(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text('stage = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not an array"):
        load_registry(p)


def test_entry_missing_name(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text('[[stage]]\ndescription = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="missing 'name'"):
        load_registry(p)
