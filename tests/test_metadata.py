# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""Contract and metadata tests for ocean-resources."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_version_freeze_and_pep639():
    pyproject_path = REPO_ROOT / "pyproject.toml"
    assert pyproject_path.exists(), "pyproject.toml must exist"

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    # Version freeze per T-20260920-167562623
    assert project.get("version") == "0.2.0", "Version must be 0.2.0 (frozen)"

    # PEP 639 License conformity
    license_val = project.get("license")
    assert license_val == "MIT", "License must be MIT"

    classifiers = project.get("classifiers", [])
    for c in classifiers:
        assert not c.startswith("License ::"), (
            f"PEP 639 violation: redundant/contradictory license classifier found: {c}"
        )


def test_mandatory_governance_and_discoverability_files_exist():
    mandatory_files = [
        "README.md",
        "README_de.md",
        "CHANGELOG.md",
        "LICENSE",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "llms.txt",
        "llm.txt",
        "SKILL.md",
        "pyproject.toml",
    ]
    for filename in mandatory_files:
        file_path = REPO_ROOT / filename
        assert file_path.exists(), f"Mandatory file {filename} is missing"
        assert file_path.stat().st_size > 0, f"Mandatory file {filename} must not be empty"


def test_bilingual_readme_bilateral_anchor_parity():
    en_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    de_readme = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")

    anchors = [f"sec-0{i}" for i in range(1, 9)]
    for anchor in anchors:
        assert f'id="{anchor}"' in en_readme, f"Anchor {anchor} missing in README.md"
        assert f'id="{anchor}"' in de_readme, f"Anchor {anchor} missing in README_de.md"


def test_public_api_exports():
    import ocean_resources

    assert hasattr(ocean_resources, "__all__"), "ocean_resources must define __all__"
    for symbol in ocean_resources.__all__:
        assert hasattr(ocean_resources, symbol), f"Exported symbol {symbol} missing in package"


def test_mermaid_diagram_syntax_guardrails():
    for md_file in REPO_ROOT.glob("*.md"):
        content = md_file.read_text(encoding="utf-8")
        mermaid_blocks = re.findall(r"```mermaid(.*?)```", content, re.DOTALL)
        for block in mermaid_blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            for line in lines:
                if line.startswith("flowchart") or line.startswith("sequenceDiagram"):
                    continue
                # In sequence diagrams, semicolons in messages are forbidden
                if "->" in line:
                    assert ";" not in line, f"Semicolon in sequence diagram message: {line}"
