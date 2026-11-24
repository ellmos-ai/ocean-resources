# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""Contract, security-gate, and metadata tests for ocean-resources."""

from __future__ import annotations

from fnmatch import fnmatchcase
from importlib.resources import files
import json
from pathlib import Path
import re
import subprocess
import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_version_freeze_and_pep639():
    pyproject_path = REPO_ROOT / "pyproject.toml"
    assert pyproject_path.exists(), "pyproject.toml must exist"

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    assert project.get("version") == "0.2.0", "Version must be 0.2.0 (frozen)"
    assert project.get("dependencies") == [], "Default runtime dependencies must remain empty (stdlib-only)"

    # PEP 639 License & SBOM conformity
    license_val = project.get("license")
    assert license_val == "MIT", "License must be MIT"
    license_files = project.get("license-files", [])
    assert "LICENSE" in license_files, "license-files must include LICENSE"
    assert "THIRD_PARTY_LICENSES.md" in license_files, "license-files must include THIRD_PARTY_LICENSES.md"

    classifiers = project.get("classifiers", [])
    assert classifiers, "PEP 621 classifiers should be populated"
    assert "Typing :: Typed" in classifiers, "PEP 561 Typing :: Typed classifier required"
    for c in classifiers:
        assert not c.startswith("License ::"), (
            f"PEP 639 violation: redundant/contradictory license classifier found: {c}"
        )

    urls = project.get("urls", {})
    for key in ("Homepage", "Documentation", "Repository", "Issues", "Changelog", "Security", "Code of Conduct"):
        assert key in urls and urls[key].startswith("https://"), f"Missing or invalid project URL: {key}"

    package_data = data.get("tool", {}).get("setuptools", {}).get("package-data", {})
    pkg_files = package_data.get("ocean_resources", [])
    for name in ("manifest.json", "pipeline-catalog.json", "py.typed"):
        assert any(fnmatchcase(name, pattern) for pattern in pkg_files), f"{name} must be covered by package-data"

    pytest_cfg = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert pytest_cfg.get("xfail_strict") is True, "pytest xfail_strict must be enabled"
    addopts = pytest_cfg.get("addopts", [])
    assert "--strict-markers" in addopts, "pytest --strict-markers must be enabled"
    for opt in addopts:
        assert "--basetemp" not in opt, "Relative --basetemp is forbidden in pyproject.toml"


def test_mandatory_governance_and_discoverability_files_exist():
    mandatory_files = [
        "README.md",
        "README_de.md",
        "CHANGELOG.md",
        "TODO.md",
        "LICENSE",
        "THIRD_PARTY_LICENSES.md",
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

    assert (REPO_ROOT / "ocean_resources" / "py.typed").is_file(), "PEP 561 py.typed marker must exist"


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
        "setuptools>=77.0.3",
        "--sdist",
        "--wheel",
        "pytest",
    ):
        assert expected in ci_text, f"CI workflow missing required element: {expected}"


def test_dependabot_and_issue_templates_exist():
    dependabot_path = REPO_ROOT / ".github" / "dependabot.yml"
    assert dependabot_path.exists(), ".github/dependabot.yml must exist"
    dep_text = dependabot_path.read_text(encoding="utf-8")
    assert 'package-ecosystem: "github-actions"' in dep_text
    assert 'package-ecosystem: "pip"' in dep_text
    limits = [int(v) for v in re.findall(r"open-pull-requests-limit:\s*(\d+)", dep_text)]
    assert len(limits) >= 2 and all(limit > 0 for limit in limits), (
        "All Dependabot ecosystems must allow > 0 open PRs"
    )

    for template_name in ("bug_report.md", "feature_request.md"):
        tpl = REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / template_name
        assert tpl.exists(), f"Missing issue template: {template_name}"
        assert tpl.stat().st_size > 0


def test_security_policy_slas_and_sbom_parity():
    sec_text = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    for token in ("48 Stunden", "5 Werktagen", "30 Kalendertage", "48 hours", "5 business days", "30 calendar days"):
        assert token in sec_text, f"SECURITY.md missing required SLA target: {token}"
    assert "not yet enabled" in sec_text and "noch nicht aktiviert" in sec_text, (
        "SECURITY.md must retain honest fallback instructions when PVR is not enabled"
    )

    sbom_text = (REPO_ROOT / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
    for dep in ("PSF-2.0", "psutil", "BSD-3-Clause", "setuptools", ">= 77.0.3", "mlx", "numpy", "nanobind", "build"):
        assert dep in sbom_text, f"THIRD_PARTY_LICENSES.md missing SBOM entry: {dep}"

    todo_text = (REPO_ROOT / "TODO.md").read_text(encoding="utf-8")
    assert "## STATUS" in todo_text and "| Category | Status | Notes |" in todo_text

    mod_meta = json.loads((REPO_ROOT / "ellmos-module.v2.json").read_text(encoding="utf-8"))
    skill_text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert mod_meta.get("visibility") == "public"
    assert "visibility: public" in skill_text


def test_gitignore_lock_and_data_gate_defense():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        "__pycache__/",
        "*.pyc",
        ".env",
        ".env.*",
        "!.env.example",
        "*.pem",
        "*.key",
        "*.db",
        "*.sqlite*",
        "*.bachdb*",
        "data/",
        "data/temp/**",
        "*export*.csv",
        "*dump*",
        "inbox_config*",
        "TO-DECIDE-USER*",
        ".idea/",
        ".vscode/",
        "LOCK.txt",
        "LOCK.*.txt",
        "LOCK-CACHE.*",
        "*MARKETING-LOG.txt",
        "*.sync-conflict-*",
        "CONFLICT_REVIEW_LOG*",
        ".venv/",
        ".ruff_cache/",
        ".coverage",
    ):
        assert pattern in gitignore, f".gitignore must ignore {pattern}"


def test_no_hardcoded_local_paths_or_secrets_in_tracked_files():
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    forbidden = re.compile(
        r"(?i)(c:[\\/]+users[\\/]+|c:[\\/]+_local_dev|/users/[a-z0-9_-]+/|/home/(?!runner/)[a-z0-9_-]+/|"
        r"\bt-20\d{6}-\d+\b|\bwc-20\d{6}-|sec-bach-rescue-tag|\bpb-\d+\b|ponytail:|"
        r"ghp_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
    )
    this_file = Path(__file__).resolve()
    for rel in result.stdout.splitlines():
        path = (REPO_ROOT / rel).resolve()
        if path == this_file or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = forbidden.search(text)
        assert match is None, f"Forbidden local path or secret pattern in {rel}: {match.group(0)!r}"


def test_llm_installation_guidance_present():
    en_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    de_readme = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")
    llms_txt = (REPO_ROOT / "llms.txt").read_text(encoding="utf-8")

    assert "### Install with your LLM" in en_readme
    assert "Install and verify https://github.com/ellmos-ai/ocean-resources" in en_readme
    assert "### Mit deinem LLM installieren" in de_readme

    for doc_name, content in (("README.md", en_readme), ("README_de.md", de_readme)):
        assert "llms.txt" in content and "SKILL.md" in content, f"{doc_name} must reference llms.txt and SKILL.md"
        assert "THIRD_PARTY_LICENSES.md" in content, f"{doc_name} must reference THIRD_PARTY_LICENSES.md"
        assert not re.search(r"\bcurl\s+[^\n`]+\|\s*(?:sh|bash)\b", content, re.IGNORECASE), (
            f"{doc_name} must not recommend curl|sh"
        )

    for section in ("Prerequisites", "Install", "Verification", "python -m pytest"):
        assert section in llms_txt, f"llms.txt missing machine-readable guidance: {section}"


def test_packaged_json_resources_and_cli_smoke():
    pkg = files("ocean_resources")
    manifest_data = json.loads(pkg.joinpath("manifest.json").read_text(encoding="utf-8"))
    catalog_data = json.loads(pkg.joinpath("pipeline-catalog.json").read_text(encoding="utf-8"))
    assert pkg.joinpath("py.typed").is_file(), "py.typed must be accessible via importlib.resources"

    assert manifest_data.get("schema") == "ellmos.ocean-resource-seam.v1"
    assert catalog_data.get("schema") == "ocean-pipeline-catalog.v1"

    from ocean_resources.__main__ import main

    assert main(["manifest"]) == 0
    assert main(["catalog"]) == 0
