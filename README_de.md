# ocean-resources — RAM-Distributor & Modell-Runtime-Scheduler

[![Version: 0.2.0](https://img.shields.io/badge/version-0.2.0-blue.svg)](pyproject.toml)
[![CI](https://github.com/ellmos-ai/ocean-resources/actions/workflows/ci.yml/badge.svg)](.github/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Tests: Passing](https://img.shields.io/badge/tests-passing%20%7C%20100%25%20green-success.svg)](tests/)
[![Verified: 2026-11-27](https://img.shields.io/badge/verified-2026--11--27-brightgreen.svg)](tests/test_metadata.py)
[![LLM Ready: llms.txt](https://img.shields.io/badge/llm-ready-informational.svg)](llms.txt)

> **Sprachen / Languages:** [Deutsch](README_de.md) | [English](README.md)

Herausgelöstes Ocean-Ressourcenmodul (`#2441`, `#1949`, `#2027`, `#2028`, `#2152`, `#2308`–`#2311`, `#2334`, `#2419`):
Einheitliche Koordination von physischem Host-Arbeitsspeicher (RAM/VRAM), dynamischer Modell-Residenz und prozessübergreifender KI-Inferenz-Arbitrierung.

---

## Schnellnavigation / Table of Contents

- [<a href="#sec-01">01. Architektur-Grundsatz & Zweistufiges Claiming</a>](#sec-01)
- [<a href="#sec-02">02. SOLL-Stand vs. IST-Stand</a>](#sec-02)
- [<a href="#sec-03">03. Die 4 Prioritätsklassen nach Nutzernähe (UserProximity)</a>](#sec-03)
- [<a href="#sec-04">04. Visuelle Architektur-Topologie & Sequenzdiagramme</a>](#sec-04)
- [<a href="#sec-05">05. Praxis-Rezepte (GUI, HTTP API, Python)</a>](#sec-05)
- [<a href="#sec-06">06. Sicherheit, Invarianten & Level 1 SBOM</a>](#sec-06)
- [<a href="#sec-07">07. Installation & Testausführung</a>](#sec-07)
- [<a href="#sec-08">08. Governance & Mitwirken</a>](#sec-08)

---

<a id="sec-01"></a>
## 01. Architektur-Grundsatz & Zweistufiges Claiming

> **Grundsatz (Entscheidung 24.10.2026):** Keine starren Modell-Slots mehr. **Agenten sind direkt an Modelle gebunden** (`model` + `model_fallbacks`). Zur Laufzeit erfolgt ein **zweistufiges Claiming**:

1. **Stufe 1: RAM-Claim (`RAMDistributor` / Hauptfackel):** Welcher Großverbraucher (lokales 27B-Modell vs. wissenschaftliche Compute-Pipeline vs. Media-Job) darf physischen RAM/VRAM belegen?
2. **Stufe 2: Modell-Runtime-Claim (`Modell-Runtime` / Modellfackel):** Welcher Agent oder Prozess erhält den nächsten Inferenz-Turn auf dem geladenen Modell – nach hostseitiger Abbildung der **4 Nutzernähe-Klassen (`UserProximity`)** auf Vordergrund-/Hintergrundtickets und der bedingten **3:1-Auswahlregel (`inference_queue_policy`)**?

---

<a id="sec-02"></a>
## 02. SOLL-Stand vs. IST-Stand (Stand 27.11.2026)

| Bereich | SOLL-Stand (Zielarchitektur) | Belegter IST-Stand |
|---|---|---|
| **Stufe 1: RAM-Distributor (`ram_distributor.py`)** | Einziger Host-Scheduler für RAM-Fackel, Modell-Residenz und Compute-Jobs; Verbraucher melden `ConsumerCapability`; Watchdog ist reiner Sensor/Aktor mit `WatchdogHysteresisPolicy`. | Implementiert & getestet in `ocean_resources/ram_distributor.py` (`RAMDistributor`, `ConsumerCapability`, `UserProximity`, `DistributorRequest`, `WatchdogHysteresisPolicy`, `FallbackDecision`); über `ocean_stack_bridge.py` in `open-ocean` und `bach` gebunden (`bound: true`). |
| **Stufe 2: Modell-Runtime & Queue (`inference_queue_policy.py` & `HostInferenceGate`)** | Prozessübergreifende, faire Zulassung (`select_next_ticket`, 3:1 Foreground-Fairness) + automatische Fallback-Kette (`cloud_router`, `smaller_local_model`, `defer_with_checkpoint`). | `select_next_ticket` ist eine reine Auswahlfunktion über einen vom Host gelieferten Schnappschuss. Sie besitzt keine Queue, Prozesslocks, Modelllade-, Fallback- oder Dispatch-Ausführung. BACH `HostInferenceGate` ist ein externer Verbraucher; seine Auslieferung wird durch diese Modultests nicht belegt. |
| **Abkühlzulassung vor jedem Lauf (`RunCooldownGate`)** | Vor jedem Lauf eines lokalen Großverbrauchers entscheidet der Verteiler, ob er starten darf; unter Last pausiert der Verbraucher und fährt nach der Abkühlung wieder an. | `RunCooldownGate.check()` mit nur lesendem `sample_stress()` (`vm_stat`-Swapouts, `kern.memorystatus_vm_pressure_level`, `pmset -g therm`): Swap-Schreibrate über 32 MiB/min oder Druck über normal -> `cooldown:swap`, Wärmewarnung -> `cooldown:thermal`, weniger als 60 s seit dem Lauf -> `cooldown:min`. Fällt eine Messung aus, entfällt nur ihr Kriterium. Standardwerte in `RunCooldownPolicy`. |
| **Agenten-Modell-Bindung (statt alter Slots)** | Alle Agenten („Deine Agenten") sind gleichberechtigte Profile mit Modellbindung (`model` + `model_fallbacks`) und Nutzernähe-Klasse 1–4. Historischer Schlüssel `slots` in `slots_config.json` dient nur der Speicher-Abwärtskompatibilität. | `model_sockets.py` (`bach.model-sockets.v1`) projiziert und verwaltet Agenten-Modell-Bindungen; Tray (`ellmos-tray`) und GUI (`/agenten/running`) zeigen **„🤖 Deine Agenten (Modell-Bindung)"** und **„🔥 RAM-Distributor & Modell-Claim"**. |
| **Hardware- & RAM-gekoppeltes Modell-Tuning (`model_tuning_catalog.py`)** | Konfigurationsfreie Sicherheits-Defaults für Kontextfenster (`num_ctx`), KV-Cache-Quantisierung, Flash-Attention und Clutch-Gänge gekoppelt an den physischen Host-RAM (`ocean.model-tuning-catalog.v1`). | Implementiert & getestet in `ocean_resources/model_tuning_catalog.py` (`load_model_tuning_catalog`, `resolve_model_tuning`, `detect_host_ram_gib`, `export_clutch_local_gears`) nach Vorrangkaskade `slot_override > model_config > catalog_default > env_fallback > safe_baseline`. |

---

<a id="sec-03"></a>
## 03. Die 4 Prioritätsklassen nach Nutzernähe (`UserProximity`)

| Klasse | Enum (`UserProximity`) | Bedeutung | Beispiele |
|---|---|---|---|
| **Klasse 1** | `DIRECT_INTERACTIVE = 1` | **Interaktiv:** Der Nutzer wartet gerade direkt auf Antwort. Wird niemals durch die Tray-Tendenz herabgestuft. | GUI-Chat (`buddha_chat`), `ellmos-talk` Funkgerät (PTT/Duplex), direkte Konsolenanfrage |
| **Klasse 2** | `DELEGATED_FOREGROUND = 2` | **Aktiver Nutzerauftrag (vererbt):** Vom Klasse-1-Chat oder Nutzer aktiv an einen Experten/Subagenten delegierter Auftrag (`inherited_from_proximity`). | Beauftragter Experte, Telegram/Signal-Connector-Auftrag (`buddha_connector`) |
| **Klasse 3** | `SCHEDULED_ROUTINE = 3` | **Geplant / Terminiert:** Fälliger Kalender-Terminagent oder wiederkehrender Routine-Agent. | Terminagent, geplanter Review-Lauf |
| **Klasse 4** | `AUTONOMOUS_BACKGROUND = 4` | **Hintergrund:** Autonome Dauer- oder Leerlaufarbeit ohne akuten Nutzerbezug. | `buddha_always_on`, autonome Task-Master-Abfrage |

---

<a id="sec-04"></a>
## 04. Visuelle Architektur-Topologie & Sequenzdiagramme

Die Diagramme zeigen die angestrebte Host-Integration, keine verifizierte Laufzeitverdrahtung. Queue-Verwaltung, Zulassung und Modelloperationen liegen beim Verbraucher. `QueueTicket` kennt nur foreground/background; die Zuordnung der vier Nutzernähe-Klassen erfolgt im Host.

### System-Architektur Topologie

```mermaid
flowchart TD
    subgraph HostHardware["Host-System & Hardware-Ressourcen"]
        PhysicalRAM["Physischer RAM / Unified Memory (z. B. 32-64 GiB)"]
        Sensors["Speicherdruck-Sensoren / Memwatchdog"]
    end

    subgraph Stage1["Stufe 1: RAMDistributor (Physischer Speicherscheduler)"]
        Distributor["RAMDistributor (Host-Scheduler)"]
        Hysteresis["WatchdogHysteresisPolicy (Kaltstart-Schutzfenster 90s)"]
        Tendency{"Scheduler-Tendenz<br/>(ollama vs compute)"}
        Capabilities["ConsumerCapability Register"]
    end

    subgraph Stage2["Stufe 2: Modell-Runtime & Inferenz-Queue"]
        QueuePolicy["InferenceQueuePolicy (select_next_ticket)"]
        FairnessRatio["3:1 Vordergrund-Fairness-Verhältnis"]
        InferenceGate["HostInferenceGate (~/.bach/runtime/local-inference/)"]
    end

    subgraph Consumers["Verbraucher & Nutzernähe-Klassen"]
        P1["Klasse 1: DIRECT_INTERACTIVE (UI-Chat, Funkgerät PTT)"]
        P2["Klasse 2: DELEGATED_FOREGROUND (Beauftragte Experten)"]
        P3["Klasse 3: SCHEDULED_ROUTINE (Geplante Routine-Agenten)"]
        P4["Klasse 4: AUTONOMOUS_BACKGROUND (Dauerhafter Leerlauf-Worker)"]
        ComputeJobs["Externe Compute-Pipelines (z. B. NemoFold, PyTorch)"]
    end

    P1 & P2 & P3 & P4 -->|"Validierten Ticket-Schnappschuss liefern"| QueuePolicy
    ComputeJobs -->|"Ressourcen-Claim"| Distributor
    QueuePolicy --> FairnessRatio
    FairnessRatio --> InferenceGate
    InferenceGate -->|"Modell-Turn Zuteilung"| PhysicalRAM
    Distributor --> Tendency
    Tendency --> Capabilities
    Capabilities --> PhysicalRAM
    Sensors --> Hysteresis
    Hysteresis -->|"Sichere Eviction-Entscheidung"| Distributor
```

### Zweistufiger Claiming- & Ausführungs-Lebenszyklus

```mermaid
sequenceDiagram
    autonumber
    actor User as Nutzer / Agentenprozess
    participant Gate as HostInferenceGate
    participant Policy as InferenceQueuePolicy
    participant Dist as RAMDistributor
    participant Hardware as Physischer RAM / VRAM

    User->>Dist: ConsumerCapability anmelden (weights_ram_bytes, kv_reserve)
    User->>Gate: Modell-Turn anfordern (Nutzernähe: Klasse 1 oder 2)
    Gate->>Dist: RAM-Verfügbarkeit & Tendenz prüfen
    alt RAM ausreichend oder Modell bereits resident
        Dist-->>Gate: Zulassung erteilt (admitted_local)
    else Zu wenig RAM & verdrängbar
        Dist-->>Gate: Fallback-Entscheidung (cloud_router oder smaller_model)
    end
    Gate->>Policy: Host-eigene lebende Tickets und foreground_streak liefern
    Policy->>Policy: 3:1 Verhältnis Vordergrund zu Hintergrund auswerten
    Policy-->>Gate: Nächstes Ticket auswählen (Grund: foreground_priority)
    Gate->>Hardware: Inferenz-Turn auf Hardware ausführen
    Hardware-->>Gate: Turn abgeschlossen
    Gate-->>User: Ergebnis-Stream zurückgeben
    Gate->>Dist: Host-eigene Residenzbuchführung (Integrationsziel)
```

---

<a id="sec-05"></a>
## 05. Praxis-Rezepte (GUI, HTTP API, Python)

### Fall A: Externer Prozess braucht RAM / VRAM (Compute-Job, Training, eigenes Modell)

1. **Per 1-Klick im System-Tray (`ellmos-tray`) oder in der GUI (`/agenten/running`):**
   - Tray-Menü → **`🔥 RAM-Distributor & Modell-Claim`** → **`🔓 Modell & RAM jetzt für andere Prozesse freigeben (Compute-Vorrang + Unload)`**
   - Schaltet die Tendenz auf `compute`, pausiert `buddha_always_on` und entlädt das aktive Ollama-Modell sofort (`keep_alive: 0`).

2. **Per HTTP-API (`:8081` & `:11434`) aus beliebigen Skripten:**
   ```bash
   # 1. Tendenz im RAM-Distributor auf Compute-Vorrang setzen
   curl -X POST http://127.0.0.1:8081/api/fackel \
     -H "Content-Type: application/json" \
     -d '{"preference": "compute"}'

   # 2. Geladenes Ollama-Modell sofort aus dem VRAM/RAM entladen (keep_alive = 0)
   curl -X POST http://127.0.0.1:11434/api/generate \
     -d '{"model": "qwen3.8:27b-mlx", "keep_alive": 0}'
   ```

3. **Programmatisch über `ocean_resources.ram_distributor` (Python):**
   ```python
   from ocean_resources.ram_distributor import (
       ConsumerCapability,
       DistributorRequest,
       RAMDistributor,
       UserProximity,
   )

   dist = RAMDistributor(total_ram_bytes=32 * 1024**3, tendency="compute")
   dist.register_consumer(
       ConsumerCapability(
           consumer_id="nemofold-pipeline",
           kind="compute",
           weights_ram_bytes=18 * 1024**3,
           can_yield_at_turn_boundary=False,
           can_checkpoint=True,
           fallback_chain=("defer_with_checkpoint",),
       )
   )
   decisions = dist.evaluate(
       [DistributorRequest(request_id="req-1", consumer_id="nemofold-pipeline", proximity=UserProximity.DELEGATED_FOREGROUND)],
       available_ram_bytes=24 * 1024**3,
   )
   ```

4. **Über den Host-Compute-Lock (`~/.memwatchdog/compute_active.lock`):**
   Langlaufende Forschungs-/Compute-Jobs tragen ihre PID in `~/.memwatchdog/compute_active.lock` ein (`{"active_compute_jobs": [{"pid": 12345, "name": "simulation"}]}`). Solange dieser Lock aktiv ist und die Tendenz auf `compute` steht, startet der Hintergrund-Worker keine Modell-Loads.

---

### Fall B: Prozess oder Agent will ein lokales Modell nutzen / claimen (Modell-Runtime)

1. **Kooperativer Turn-Claim über `HostInferenceGate` (Python, prozessübergreifend):**
   Alle lokalen Prozesse auf dem Host teilen sich `~/.bach/runtime/local-inference/`. Mit `priority="foreground"` (Klasse 1/2) überholt dein Prozess laufende Hintergrund-Worker (`background`, Klasse 4) automatisch an der nächsten sicheren Turn-Grenze:
   ```python
   from hub._services.chat.host_inference_gate import HostInferenceGate

   async with HostInferenceGate().turn("mein-agent-prozess", "foreground"):
       # Exklusiver Modell-Claim: Hintergrund-Worker warten automatisch.
       # Stürzt der Prozess ab, gibt der OS-Flock/PID-Check das Gate sofort frei.
       ...
   ```

2. **Direkter Modell-Aufruf mit Vordergrund-Claim über die Control API (`:8081/api/chat`):**
   ```bash
   curl -X POST http://127.0.0.1:8081/api/chat \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer $(cat ~/.credentials/bach_control_api_token)" \
     -d '{"prompt": "Analysiere diesen Befund...", "chat_id": "mein-externer-agent"}'
   ```

3. **Modell-Bindung eines Agenten umstellen (ohne starre Slots):**
   ```bash
   # Über die BACH-CLI:
   bach model-sockets list --json
   bach model-sockets catalog --json

   # Oder per HTTP-API (Agentenprofil auf anderes Modell umbinden):
   curl -X POST http://127.0.0.1:8081/api/slots \
     -H "Content-Type: application/json" \
     -d '{"slot_id": "buddha_chat", "updates": {"model": "qwen3.8:27b-mlx", "backend": "ollama"}}'
   ```

---

<a id="sec-06"></a>
## 06. Sicherheit, Invarianten & Level 1 SBOM

- **INV-LOCAL-01 (Zero Egress):** Reines lokales Scheduling ohne externe Netzwerk-Egress-Abhängigkeiten.
- **INV-LOCAL-02 (Deterministische Queue-Auswahl):** Wenn beide Prioritäten gültige lebende Tickets haben, der Host `next_foreground_streak` fortschreibt und `legacy_foreground_pending` falsch ist, wählt die Funktion nach höchstens drei aufeinanderfolgenden Vordergrundauswahlen ein Hintergrundticket. Bei gesetzter Legacy-Vordergrundsperre wird kein Ticket gewählt. Das ist keine zeitliche oder durchgängige Starvation-Garantie.
- **INV-LOCAL-03 (Unprivilegierte Ausführung):** Keine Administrator- oder Root-Rechte erforderlich; Ausführung erfolgt im unprivilegierten Benutzerkontext.
- **Level 1 SBOM ([THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)):**
  - **Laufzeit (`dependencies = []`):** `0` externe Pflichtabhängigkeiten — Python `>= 3.11` Standardbibliothek (`PSF-2.0`)
  - **Optionales Native-Extra (`ocean-resources[native]`):** `psutil >= 5.9.6, < 8` (`BSD-3-Clause`)
  - **Optionale Apple-Silicon-MLX-Quellerweiterung (`native/mlx`):** `mlx == 0.32.0` (`MIT`), `mlx-lm == 0.31.3` (`MIT`), `numpy` (`BSD-3-Clause`), `nanobind == 2.13.0` (`BSD-3-Clause`)
  - **Build- & Entwicklungswerkzeuge:** `setuptools >= 77.0.3` (`MIT`), `build` (`MIT`), `pytest >= 8.0` (`MIT`), `ruff >= 0.4` (`MIT`)
  - **Lizenz:** MIT (`SPDX-License-Identifier: MIT`)

---

<a id="sec-07"></a>
## 07. Installation & Testausführung

```bash
# 1. Repository klonen
git clone https://github.com/ellmos-ai/ocean-resources.git
cd ocean-resources

# 2. Paket im Entwicklungsmodus in isolierter Umgebung (.venv) installieren
python -m venv .venv
python -m pip install -e ".[dev]"

# 3. Testsuite, Linter und Offline-CLI-Prüfung ausführen
python -m pytest
ruff check .
python -m compileall -q ocean_resources tests
python -m ocean_resources manifest
```

### Mit deinem LLM installieren / Install with your LLM

```text
Installiere und verifiziere https://github.com/ellmos-ai/ocean-resources (Release-Tag/Commit-Pin bevorzugen, keine ungeprüften curl|sh-Pipes):
1. Lies zuerst llms.txt und SKILL.md im Repo-Root.
2. Erstelle eine isolierte Umgebung (.venv) und führe den Installationsbefehl aus llms.txt aus.
3. Prüfe die Installation mit dem Verifikationsbefehl (python -m pytest && python -m ocean_resources manifest) und berichte das Ergebnis.
```

---

<a id="sec-08"></a>
## 08. Governance & Mitwirken

Beiträge und Fehlerberichte sind herzlich willkommen. Bitte beachten Sie unsere [Beitragsrichtlinie](CONTRIBUTING.md), den [Verhaltenskodex](CODE_OF_CONDUCT.md), die [Sicherheitsrichtlinie](SECURITY.md) sowie die [Drittanbieter-Lizenzen / SBOM](THIRD_PARTY_LICENSES.md).
