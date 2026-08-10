import json
from pathlib import Path

import pytest

from hermes_skill_publisher.state import (
    SCHEMA_VERSION,
    StateError,
    audit,
    list_journals,
    load_registry,
    read_audit,
    save_registry,
    state_root,
    validate_publication,
    write_journal,
)


def test_registry_roundtrip(isolated_home):
    registry = {"schema_version": SCHEMA_VERSION, "publications": {"demo": {"digest": "sha256:x"}}}
    save_registry(registry)
    assert load_registry() == registry
    assert state_root().is_relative_to(isolated_home["hermes"])


def test_corrupt_registry_blocks(isolated_home):
    root = state_root()
    root.mkdir(parents=True)
    (root / "registry.json").write_text("[]")
    with pytest.raises(StateError):
        load_registry()


def test_journal_is_durable_and_schema_checked(isolated_home):
    journal = {"schema_version": SCHEMA_VERSION, "operation_id": "demo-1", "operation": "promote"}
    write_journal(journal)
    assert list_journals() == [journal]
    (state_root() / "transactions" / "demo-1.json").write_text(json.dumps({"schema_version": 9, "operation_id": "demo-1"}))
    with pytest.raises(StateError):
        list_journals()


def test_audit_is_bounded_and_omits_unknown_content(isolated_home):
    audit("test", result="blocked", error="x" * 5000, skill_name="demo", content="secret body")
    event = read_audit(1)[0]
    assert event["error"] == "details withheld"
    assert "content" not in event
    assert "secret body" not in json.dumps(event)


def test_read_audit_zero_limit_returns_empty(isolated_home):
    audit("one", result="success")
    audit("two", result="success")
    assert read_audit(0) == []
    assert len(read_audit(2)) == 2


def _valid_record(tmp_path):
    shared = tmp_path / "shared"
    return {
        "canonical_path": str(shared / "demo-skill"),
        "source_relpath": "category/demo-skill",
        "digest": "sha256:" + "0" * 64,
        "scope": "shared",
        "created_at": "test",
        "adapter_links": {"other": {"path": str(tmp_path / "adapter" / "demo-skill"), "link_text": "../shared/demo-skill"}},
    }


def test_validate_publication_accepts_valid_record(tmp_path):
    assert validate_publication("demo-skill", _valid_record(tmp_path))["scope"] == "shared"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record.update(canonical_path="relative/demo-skill"),
        lambda record: record.update(canonical_path="/tmp/other-name"),
        lambda record: record.update(source_relpath="../escape/demo-skill"),
        lambda record: record.update(source_relpath="category/other-name"),
        lambda record: record.update(digest="sha256:not-hex"),
        lambda record: record.update(scope="local"),
        lambda record: record.update(adapter_links=[]),
        lambda record: record["adapter_links"]["other"].update(path="/tmp/adapter/wrong-name"),
        lambda record: record["adapter_links"]["other"].update(link_text="/abs/target/demo-skill"),
        lambda record: record["adapter_links"]["other"].update(link_text="../shared/other-name"),
    ],
)
def test_validate_publication_rejects_malformed_records(tmp_path, mutate):
    record = _valid_record(tmp_path)
    mutate(record)
    with pytest.raises(StateError):
        validate_publication("demo-skill", record)
