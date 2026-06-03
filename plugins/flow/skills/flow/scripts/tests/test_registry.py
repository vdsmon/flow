from pathlib import Path

import pytest

from _registry import load_registry, registry_by_name

REAL_REGISTRY = Path(__file__).resolve().parent.parent.parent / "stage-registry.toml"


def test_load_real_registry():
    entries = load_registry(REAL_REGISTRY)
    names = [e.name for e in entries]
    assert "ticket" in names
    assert "commit" in names
    assert "reflect" in names


def test_registry_by_name_fields():
    by = registry_by_name(REAL_REGISTRY)
    assert by["commit"].required_fields == ["commit_type", "commit_summary"]
    assert "records_diff_baseline" in by["implement"].roles
    assert by["implement"].default_timeout_min == 30


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
