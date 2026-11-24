# TODO & Release-Gate-Status — `ocean-resources`

## STATUS

| Category | Status | Notes |
|---|---|---|
| 1. `.gitignore` & Pre-Push Data-Gate | PASS | Enthält Python-/Build-Artefakte, `.env*`, Schlüssel/Zertifikate, `*.db`/`*.sqlite*`/`*.bachdb*`, `data/`, `*export*.csv`, `*dump*`, `inbox_config*`, `TO-DECIDE-USER*` und Multi-Host-Sync-Konfliktschutz. |
| 2. Bilingual Documentation (`README.md` / `README_de.md`) | PASS | Englisch als primäre `README.md`, vollständige deutsche Paralleldokumentation in `README_de.md` mit bilateraler Anker-Parität (`sec-01`–`sec-08`). |
| 3. License & PEP 639 Metadata | PASS | `MIT` (`LICENSE` + `THIRD_PARTY_LICENSES.md`), PEP-639-konform (`setuptools >= 77.0.3`), PEP-561-Marker (`ocean_resources/py.typed`). |
| 4. Database & Runtime State Hygiene | PASS | Keine `.db`-, `.sqlite*`- oder Laufzeit-Journaldateien im Git-Index getrackt. |
| 5. Environment & Credential Hygiene | PASS | Keine `.env`-Dateien, API-Schlüssel, Bearer-Tokens oder privaten Zertifikate versioniert. |
| 6. Secret & Token Scan | PASS | Vollständiger Scan aller getrackten Dateien ohne Befund. |
| 7. Personal & Local Path Hygiene | PASS | Keine hartcodierten Benutzer- oder Workstation-Pfade in Code oder Dokumentation. |
| 8. PII & Internal Metadata Hygiene | PASS | Keine privaten E-Mail-Adressen oder internen Hostnamen; `SKILL.md` und `ellmos-module.v2.json` konsistent auf `public`. |
| 9. Standalone Packaging & Zero Mandatory Dependencies | PASS | Keine internen BACH-Konfigurationsdateien getrackt; `dependencies = []` (reiner Stdlib-Import ohne installierte optionale Extras verifiziert). |
| 10. Security Policy, SBOM & CI Matrix | PASS | `SECURITY.md` mit verbindlichen SLAs (`48h` / `5d` / `30d`), `THIRD_PARTY_LICENSES.md` (Level-1 SBOM), Dependabot (`github-actions` + `pip`) und 3×3-OS/Python-CI-Matrix aktiv. |

---

## Offene Architektur- & Plattform-Aufgaben / Open Roadmap Items

- [ ] **GitHub Private Vulnerability Reporting aktivieren (Repository-Admin):** In den GitHub-Repository-Einstellungen von `ellmos-ai/ocean-resources` Private Vulnerability Reporting aktivieren (`Security -> Private vulnerability reporting`), sodass Sicherheitsmeldungen direkt über GitHub Advisories eingereicht werden können.
- [ ] **Nativer Windows-ACL-Prüfer für `ConfiguredHost`:** `ConfiguredHost` verweigert auf Windows derzeit bewusst das Laden privater Token-/Host-Konfigurationen (`fail-closed`), bis eine geprüfte native Windows-DACL-Verifikation (analog zu POSIX `0600`/`0700`) vorliegt.
- [ ] **Nativer Windows-Inode-/Handle-Reader für `LOAD_PENDING`:** `model_dispatch.require_dispatch_permission` nutzt POSIX-`flock`/Inode-Fencing und bleibt unter Windows für gestuftes Modell-Laden geschlossen, bis ein gleichwertiger Windows-File-ID-/Lock-Nachweis spezifiziert und abgenommen ist.
- [ ] **Kalibrierung der `RunCooldownPolicy`-Schwellenwerte im Dauerbetrieb:** Langzeitbeobachtung der 32-MiB/min-Swap-Schranke und der 60-s-Mindestpause auf Apple-Silicon-Workstations mit großem Unified Memory.
