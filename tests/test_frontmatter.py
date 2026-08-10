from pathlib import Path

import pytest

from hermes_skill_publisher.frontmatter import (
    FrontmatterError,
    classify_content,
    parse_document,
    rewrite_scope,
    validate_skill_file,
)
from conftest import skill_text


@pytest.mark.parametrize("scope", ["shared", "local", "private"])
def test_classifies_exact_values(scope):
    result = classify_content(skill_text("demo-skill", scope))
    assert (result.status, result.value) == ("classified", scope)


def test_missing_is_distinct():
    text = "---\nname: demo-skill\ndescription: test\n---\nBody\n"
    result = classify_content(text)
    assert result.status == "missing" and result.value is None


@pytest.mark.parametrize("value", [None, True, ["shared"], {"value": "shared"}, "project", "SHARED"])
def test_invalid_values(value):
    result = classify_content(skill_text("demo-skill", value))
    assert result.status == "invalid"


def test_alias_scalar_is_allowed_but_alias_object_is_not():
    scalar = "---\nname: demo-skill\ndescription: test\nx: &scope shared\nmetadata:\n  skill-publisher-scope: *scope\n---\n"
    mapping = "---\nname: demo-skill\ndescription: test\nx: &scope {value: shared}\nmetadata:\n  skill-publisher-scope: *scope\n---\n"
    assert classify_content(scalar).value == "shared"
    assert classify_content(mapping).status == "invalid"


@pytest.mark.parametrize("text", ["plain", "---\n[bad\n---\n", "---\n- item\n---\n"])
def test_malformed_frontmatter(text):
    assert classify_content(text).status == "invalid"


def test_portable_validation_rejects_nested_metadata(tmp_path: Path):
    package = tmp_path / "demo-skill"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: test\nmetadata:\n  hermes:\n    x: y\n  skill-publisher-scope: shared\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(FrontmatterError, match="string keys to string values"):
        validate_skill_file(package / "SKILL.md", required_scope="shared")


def test_name_must_match_directory(tmp_path: Path):
    package = tmp_path / "other-name"
    package.mkdir()
    (package / "SKILL.md").write_text(skill_text("demo-skill"), encoding="utf-8")
    with pytest.raises(FrontmatterError, match="name must match"):
        validate_skill_file(package / "SKILL.md")


def test_rewrite_scope_preserves_body_bytes():
    original = skill_text("demo-skill", "shared") + "More\n\nExact body.\n"
    changed = rewrite_scope(original, "private")
    _, body_before, _ = parse_document(original)
    _, body_after, _ = parse_document(changed)
    assert body_after == body_before
    assert classify_content(changed).value == "private"


def test_yaml_error_is_sanitized_without_source_lines():
    sentinel = "DO_NOT_LOG_THIS_SECRET"
    text = f'---\nname: demo-skill\ndescription: test\nleak: "{sentinel}\n---\n'
    result = classify_content(text)
    assert result.status == "invalid"
    assert sentinel not in result.reason
    assert "line" in result.reason


def test_deeply_nested_yaml_never_raises_from_classify():
    text = "---\nname: demo-skill\ndescription: test\n" + "x: " + "[" * 500 + "]" * 500 + "\n---\n"
    result = classify_content(text)
    assert result.status == "invalid"
    assert "RecursionError" in result.reason or "invalid" in result.reason


def test_rewrite_scope_bytes_preserves_exact_body():
    import pytest as _pytest
    from hermes_skill_publisher.frontmatter import parse_document_bytes, rewrite_scope_bytes

    frontmatter = b"---\nname: demo-skill\ndescription: test\nmetadata:\n  skill-publisher-scope: shared\n---\n"
    for body in (b"Body\r\nExact\r\n", b"lone\rcr\r", b"no final newline", "nön-ascïi body\n".encode("utf-8")):
        changed = rewrite_scope_bytes(frontmatter + body, "local")
        _, spliced, _ = parse_document_bytes(changed)
        assert spliced == body
        assert b"skill-publisher-scope: local" in changed
    with _pytest.raises(FrontmatterError):
        rewrite_scope_bytes(frontmatter + b"x", "shared")
