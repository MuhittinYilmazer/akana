"""SkillEngine F0.1 — SKILL.md parser (YAML frontmatter + markdown body)."""

from __future__ import annotations

from pathlib import Path

import pytest

from akana_server.skills.parser import (
    ParsedSkill,
    SkillParseError,
    normalize_metadata,
    parse_skill_file,
    parse_skill_md,
    split_frontmatter,
    validate_required,
)

VALID = """---
name: project_checks
type: skill
description: Test ve lint çalıştırır.
triggers:
  - "testleri çalıştır"
tags: [test, lint]
risk: low
---

# Proje test ve lint

Adımlar burada.
"""


def test_parse_valid_frontmatter_and_body() -> None:
    parsed = parse_skill_md(VALID, require_frontmatter=True)
    assert isinstance(parsed, ParsedSkill)
    assert parsed.name == "project_checks"
    assert parsed.description == "Test ve lint çalıştırır."
    assert parsed.frontmatter["type"] == "skill"
    assert parsed.frontmatter["triggers"] == ["testleri çalıştır"]
    assert parsed.frontmatter["tags"] == ["test", "lint"]
    assert parsed.body.lstrip().startswith("# Proje test ve lint")
    assert "---" not in parsed.body


def test_no_frontmatter_returns_empty_when_optional() -> None:
    parsed = parse_skill_md("# Sadece gövde\n\nMetin.\n")
    assert parsed.frontmatter == {}
    assert parsed.body.startswith("# Sadece gövde")


def test_no_frontmatter_raises_when_required() -> None:
    with pytest.raises(SkillParseError, match="does not contain a YAML frontmatter block"):
        parse_skill_md("# Sadece gövde\n", require_frontmatter=True)


def test_unclosed_frontmatter_is_descriptive() -> None:
    with pytest.raises(SkillParseError, match="no closing '---' line was found"):
        parse_skill_md("---\nname: x\ndescription: y\n# gövde\n")


def test_invalid_yaml_is_descriptive() -> None:
    text = "---\nname: [unclosed\n---\nbody\n"
    with pytest.raises(SkillParseError, match="frontmatter YAML error"):
        parse_skill_md(text, path="/tmp/SKILL.md")


def test_non_mapping_frontmatter_rejected() -> None:
    with pytest.raises(SkillParseError, match="must be a YAML mapping"):
        parse_skill_md("---\n- a\n- b\n---\nbody\n")


def test_missing_required_fields_listed() -> None:
    with pytest.raises(SkillParseError, match="missing required field.*name, description"):
        parse_skill_md("---\ntype: skill\n---\nbody\n", require_frontmatter=True)


def test_missing_description_only() -> None:
    with pytest.raises(SkillParseError, match="missing required field.*description"):
        parse_skill_md("---\nname: x\n---\nbody\n", require_frontmatter=True)


def test_invalid_type_value_rejected() -> None:
    with pytest.raises(SkillParseError, match="invalid 'type' value"):
        parse_skill_md(
            "---\nname: x\ndescription: y\ntype: banana\n---\nbody\n",
            require_frontmatter=True,
        )


def test_id_alias_maps_to_name() -> None:
    parsed = parse_skill_md(
        "---\nid: legacy_id\ndescription: y\n---\nbody\n", require_frontmatter=True
    )
    assert parsed.name == "legacy_id"


def test_string_list_field_coerced() -> None:
    parsed = parse_skill_md(
        "---\nname: x\ndescription: y\ntriggers: tek tetik\n---\nbody\n",
        require_frontmatter=True,
    )
    assert parsed.frontmatter["triggers"] == ["tek tetik"]


def test_nested_list_field_rejected() -> None:
    with pytest.raises(SkillParseError, match="must contain only plain-text items"):
        parse_skill_md(
            "---\nname: x\ndescription: y\ntags:\n  - {a: 1}\n---\nbody\n",
            require_frontmatter=True,
        )


def test_scalar_list_field_rejected() -> None:
    with pytest.raises(SkillParseError, match="must be a list or text"):
        normalize_metadata({"triggers": 42})


def test_error_includes_path() -> None:
    with pytest.raises(SkillParseError) as exc:
        parse_skill_md("---\nname: x\n", path="/skills/x/SKILL.md")
    assert "/skills/x/SKILL.md" in str(exc.value)
    assert exc.value.path == "/skills/x/SKILL.md"


def test_split_frontmatter_plain_text() -> None:
    raw, body = split_frontmatter("hello\nworld\n")
    assert raw is None
    assert body == "hello\nworld\n"


def test_validate_required_ok() -> None:
    validate_required({"name": "x", "description": "y"})  # must not raise


def test_parse_skill_file(tmp_path: Path) -> None:
    p = tmp_path / "SKILL.md"
    p.write_text(VALID, encoding="utf-8")
    parsed = parse_skill_file(p)
    assert parsed.name == "project_checks"
    assert parsed.path == str(p)


def test_parse_skill_file_missing(tmp_path: Path) -> None:
    with pytest.raises(SkillParseError, match="could not be read"):
        parse_skill_file(tmp_path / "yok" / "SKILL.md")
