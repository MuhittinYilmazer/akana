"""Open-source release hygiene — community health files + no leftover placeholders.

Guards the "ready to publish" state: the standard community files exist, the README no
longer carries the <your-repo-url> placeholder, and LICENSE keeps a real MIT header.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_community_health_files_exist() -> None:
    for rel in (
        "LICENSE",
        "README.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
    ):
        assert (REPO / rel).is_file(), f"missing community file: {rel}"


def test_license_is_mit_with_real_holder() -> None:
    txt = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in txt
    assert "Copyright (c)" in txt
    # not the unfilled MIT template placeholders
    assert "[year]" not in txt and "[fullname]" not in txt


def test_readme_has_no_publish_placeholders() -> None:
    txt = (REPO / "README.md").read_text(encoding="utf-8")
    assert "your-repo-url" not in txt, "README still has the <your-repo-url> placeholder"
    assert "on the way" not in txt, "README still promises a doc that is now present"


def test_contributing_and_security_are_actionable() -> None:
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "python akana.py test" in contributing  # tells contributors how to test
    security = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    assert "advisor" in security.lower()  # points to GitHub private advisory reporting
