# Beitragsrichtlinie / Contributing Guide

## Deutsch

Vielen Dank für Ihr Interesse, zu `ocean-resources` beizutragen!

### Wie Sie beitragen können

1. **Bug melden:** Erstellen Sie ein Issue mit dem Label `bug`
2. **Feature vorschlagen:** Erstellen Sie ein Issue mit dem Label `enhancement`
3. **Code beitragen:** Erstellen Sie einen Pull Request

### Pull Requests & Plan D Workflow

1. Forken oder klonen Sie das Repository in Ihren lokalen Entwicklungsordner (Plan D: `C:\_Local_DEV\repos\ocean-resources`).
2. Erstellen Sie einen dedizierten Feature- oder Fix-Branch: `git checkout -b feature/mein-feature` bzw. `docs/marketing-...`
3. Halten Sie die Versionsnummer (`version` in `pyproject.toml`) unverändert; Versionserhöhungen erfolgen ausschließlich im Rahmen autorisierter Release-Vorgänge (T-20260920-167562623).
4. Führen Sie vor jedem Commit alle Linter und Tests aus:
   ```bash
   ruff check .
   python -m compileall .
   python -m pytest
   git diff --check
   ```
5. Pushen Sie den Branch und eröffnen Sie einen Pull Request über `gh pr create` zur formalen Zweitabnahme.

### Lizenz- und Rechtehinweise

Pull Requests und Beiträge werden unter der MIT-Lizenz dieses Projekts bereitgestellt.

### Code-Richtlinien

- Python: PEP 8 Stil, Type Annotations (`from __future__ import annotations`), PEP 639 konforme Metadaten
- Encoding: UTF-8 für alle Dateien
- Keine hardcoded Pfade, Benutzerdaten oder API-Tokens

---

## English

Thank you for your interest in contributing to `ocean-resources`!

### How to Contribute

1. **Report bugs:** Create an issue with the `bug` label
2. **Suggest features:** Create an issue with the `enhancement` label
3. **Contribute code:** Create a Pull Request

### Pull Requests & Plan D Workflow

1. Fork or clone the repository to your local development environment (Plan D: `C:\_Local_DEV\repos\ocean-resources`).
2. Create a dedicated feature or documentation branch: `git checkout -b feature/my-feature` or `docs/...`
3. Keep the project version (`version` in `pyproject.toml`) strictly untouched; version bumps are reserved for formal release cycles (T-20260920-167562623).
4. Run all linters and test suites before committing:
   ```bash
   ruff check .
   python -m compileall .
   python -m pytest
   git diff --check
   ```
5. Push the branch and open a Pull Request via `gh pr create` for review and CI verification.

### Licensing and Contribution Terms

All contributions and pull requests are submitted under the MIT License of this repository.

### Code Guidelines

- Python: PEP 8 style, strict typing, PEP 639 metadata compliance
- Encoding: UTF-8 for all files
- Never commit hardcoded credentials, personal paths, or API keys
