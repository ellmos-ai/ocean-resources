# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""Contract and metadata tests for ocean-resources."""

from __future__ import annotations

from importlib.resources import files
import json
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
    assert "LICENSE" in project.get("license-files", []), "license-files must include LICENSE"

    classifiers = project.get("classifiers", [])
    assert classifiers, "PEP 621 classifiers should be populated"
    for c in classifiers:
        assert not c.startswith("License ::"), (
            f"PEP 639 violation: redundant/contradictory license classifier found: {c}"
        )

    urls = project.get("urls", {})
    for key in ("Homepage", "Documentation", "Repository", "Issues", "Changelog", "Security", "Code of Conduct"):
        assert key in urls and urls[key].startswith("https://"), f"Missing or invalid project URL: {key}"

    package_data = data.get("tool", {}).get("setuptools", {}).get("package-data", {})
    pkg_files = package_data.get("ocean_resources", [])
    assert "manifest.json" in pkg_files, "manifest.json must be declared in package-data"
    assert "pipeline-catalog.json" in pkg_files, "pipeline-catalog.json must be declared in package-data"

    pytest_cfg = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert pytest_cfg.get("xfail_strict") is True, "pytest xfail_strict must be enabled"
    addopts = pytest_cfg.get("addopts", [])
    assert "--strict-markers" in addopts, "pytest --strict-markers must be enabled"


def test_mandatory_governance_and_discoverability_files_exist():
    mandatory_files = [
        "README.md",
        "README_de.md",
        "CHANGELOG.md",
        "LICENSE",
        "MANIFEST.in",
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


def test_ci_workflow_matrix_and_guardrails():
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.exists(), ".github/workflows/ci.yml must exist"
    ci_text = ci_path.read_text(encoding="utf-8")

    for expected in (
        "workflow_dispatch:",
        "contents: read",
        "fail-fast: false",
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
        '"3.11"',
        '"3.12"',
        '"3.13"',
        "git diff --check",
        "ruff check",
        "compileall",
        "pytest",
    ):
        assert expected in ci_text, f"CI workflow missing required element: {expected}"


def test_dependabot_and_issue_templates_exist():
    dependabot_path = REPO_ROOT / ".github" / "dependabot.yml"
    assert dependabot_path.exists(), ".github/dependabot.yml must exist"
    dep_text = dependabot_path.read_text(encoding="utf-8")
    assert 'package-ecosystem: "github-actions"' in dep_text
    assert 'package-ecosystem: "pip"' in dep_text

    for template_name in ("bug_report.md", "feature_request.md"):
        tpl = REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / template_name
        assert tpl.exists(), f"Missing issue template: {template_name}"
        assert tpl.stat().st_size > 0


def test_gitignore_lock_and_log_defense():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        "LOCK.txt",
        "LOCK.*.txt",
        "LOCK-CACHE.*",
        "*MARKETING-LOG.txt",
        ".venv/",
        ".ruff_cache/",
        ".coverage",
    ):
        assert pattern in gitignore, f".gitignore must ignore {pattern}"


def test_llm_installation_guidance_present():
    en_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    de_readme = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")
    llms_txt = (REPO_ROOT / "llms.txt").read_text(encoding="utf-8")

    for doc_name, content in (("README.md", en_readme), ("README_de.md", de_readme)):
        assert "llms.txt" in content and "SKILL.md" in content, f"{doc_name} must reference llms.txt and SKILL.md"
        assert "curl" not in content or "| sh" not in content, f"{doc_name} must not recommend curl|sh"
        assert "Install with your LLM" in content or "Mit deinem LLM installieren" in content

    for section in ("Prerequisites", "Install", "Verification", "python -m pytest"):
        assert section in llms_txt, f"llms.txt missing machine-readable guidance: {section}"


def test_packaged_json_resources_and_cli_smoke():
    pkg = files("ocean_resources")
    manifest_data = json.loads(pkg.joinpath("manifest.json").read_text(encoding="utf-8"))
    catalog_data = json.loads(pkg.joinpath("pipeline-catalog.json").read_text(encoding="utf-8"))

    assert manifest_data.get("schema") == "ellmos.ocean-resource-seam.v1"
    assert catalog_data.get("schema") == "ocean-pipeline-catalog.v1"

    from ocean_resources.__main__ import main

    assert main(["manifest"]) == 0
    assert main(["catalog"]) == 0
