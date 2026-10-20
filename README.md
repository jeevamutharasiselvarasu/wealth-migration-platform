```markdown
# XODUS-AMP Data Migration Workbench

XODUS-AMP is a metadata-driven migration workbench. The active P0 implementation is a generic **source-to-canonical mapping** pilot limited to three entities: **Account, Household, and Client**. It supports sample-file upload, source profiling, mapping and field-lineage evidence, scope gating, human review, and MCL key/relationship validation.

**Current P0 boundary:** source and target systems are not hard-coded into the active mapping workflow; target-side loading, canonical-to-target/reverse mapping, production cutover, and wave/cohort assignment are deferred. The broader architecture sections below describe future platform direction and must not be read as evidence that those deferred capabilities are implemented.

---

## 🎨 XODUS-AMP Design & Visual System

* **Header Branding**: Navy `#152C6B` topbar with gradient `XA` logo badge and dynamic acquisition pipeline tags[cite: 108].
* **Background & Panels**: `#F4F7FC` light workspace background, crisp `#FFFFFF` cards, and `#D9E3F5` subtle structural borders.
* **Immutable Knowledge Panel**: Step 1 displays the **Built-In Wealth Kernel** explicitly marked as **`READ-ONLY (FIXED)`** to protect core platform taxonomy and mathematical invariants from accidental modification[cite: 110].

---

## 🏛️ Platform Architecture & Control/Data Plane Isolation

```text
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │                      XODUS-AMP COMMAND CENTER UI (Light Palette)                        │
 │  Step 1: Firm Knowledge  │  Step 2: Dashboard  │  Step 3: Scope  │  Step 4: Samples      │
 │  Step 5: Agent Pipeline  │  Step 6: HITL Queue│  Step 7: Reconciliation & Drift        │
 └───────────────────────────┬──────────────────────────────────┬─────────────────────────┘
                             │ REST API / Session Cookies       │ REST API / JSON
 ┌───────────────────────────▼──────────────────────────────────┴─────────────────────────┐
 │                      FASTAPI CONTROL PLANE (Metadata & Orchestration)                   │
 │  ┌─────────────────────────┐  ┌─────────────────────────────┐  ┌─────────────────────┐  │
 │  │ Wealth Kernel Taxonomy  │  │ Rulebook Compiler & Engine  │  │ SQLite DB & Audit   │  │
 │  │ (15 Fixed Entities)     │  │ (Expressions & Crosswalks)  │  │ (App_Data/db.py)    │  │
 │  └────────────┬────────────┘  └──────────────┬──────────────┘  └──────────┬──────────┘  │
 └───────────────┼──────────────────────────────┼────────────────────────────┼─────────────┘
                 │ Read Contracts               │ Input / Output             │ Artifact
 ┌───────────────▼──────────────────────────────▼────────────────────────────▼─────────────┐
 │                      AZURE DATA PLANE (Execution & Staging Engine)                     │
 │  ┌────────────────────────────────────────┐  ┌───────────────────────────────────────┐  │
 │  │ Sleeve Math Engine (Decimal \sum=1.0)  │  │ Reconciler Engine (\le 0.50% Gate)    │  │
 │  └────────────────────────────────────────┘  └───────────────────────────────────────┘  │
 └──────────────────────────────────────────────┬──────────────────────────────────────────┘
                                                │ LOAD_READY Payloads
 ┌──────────────────────────────────────────────▼──────────────────────────────────────────┐
 │                            TARGET DESTINATIONS & ARCHIVE                                │
 │  Salesforce SPARC (CRM Master)  │  Orion Eclipse (Trading)  │  ADLS Gen2 (WORM Archive) │
 └─────────────────────────────────────────────────────────────────────────────────────────┘

```

---

## ⚡ Core Invariants & Fixed Platform Standards

1. **15-Entity Canonical Taxonomy (Read-Only)**:


* **Master Entities**: Account, Household, Party, Advisor/Rep, Model, Security Master.


* **Relationship Entities**: Sleeve/Allocation, Account Owner.


* **Event & Snapshot Entities**: Position (Snapshot), Transaction, Performance Period, Tax Lot.


* **Archival Entities**: Documents & Statements, Terminated Accounts.




2. **Rep-as-PM Sleeve Math Invariant**:
* For every multi-sleeve UMA model aggregate, target weights are normalized using 4-decimal precision fixed-point arithmetic.


* **Mathematical Invariant**: $\sum_{i=1}^{n} W_{\text{sleeve}, i} = 1.0000 \quad (100.0000\%)$.


* Rounding drift adjustment ($\Delta \le 0.0005$) automatically balances the primary sleeve. Sum deviations $> 0.0005$ raise hard exceptions to trigger Step 6 HITL review.




3. **Post-Load AUM Drift Gate**:
* Staged market values are audited against legacy source extracts.


* **Variance Gate**: $V_{\text{AUM}} = \left\vert{} \frac{\text{AUM}_{\text{target}} - \text{AUM}_{\text{source}}}{\text{AUM}_{\text{source}}} \right\vert{} \times 100\% \le 0.5000\%$.


* Variances exceeding $0.5000\%$ set status to `FAIL_HOLD`, blocking cutover until SME sign-off [cite-[# 🤖 Master 9-Agent Pipeline Workflow



The platform orchestrates 9 autonomous agents across 3 execution phases:

### Phase 1: Discovery Agents

* **1. Extractor Agent (`ExtractorAgent`)**: Pulls advisor cohort extracts from Amplify and Black Diamond connectors.


* **2. Schema Mapper Agent (`SchemaMapperAgent`)**: Applies crosswalk dictionaries (e.g., `SCHW` $\rightarrow$ `SCHWAB`, `INDV_TAXABLE` $\rightarrow$ `INDIVIDUAL`).


* **3. AI Foundry Mapper (`AzureAIFoundryMapper`)**: Integrates Azure AI Foundry GPT-4o zero-shot models to infer unmapped source attributes with confidence scores.



### Phase 2: Business Logic Agents

* **4. Sleeve Math Agent (`SleeveMathAgent`)**: Enforces UMA sleeve weight decomposition and decimal sum invariants ($\sum = 1.0000$).


* **5. Compliance Validator (`ComplianceValidatorAgent`)**: Re-applies Home Office trading overlays and checks restricted security models.


* **6. Data Quality Agent (`DataQualityAgent`)**: Audits referential integrity across the canonical relationship spine (Party $\rightarrow$ Household $\rightarrow$ Account $\rightarrow$ Sleeve $\rightarrow$ Position).



### Phase 3: Operational Execution Agents

* **7. Loader Agent (`LoaderAgent`)**: Transforms records into Salesforce SPARC and Orion Eclipse staging adapter payloads.


* **8. Reconciler Agent (`ReconcilerAgent`)**: Computes post-load market value variance against the strict $\le 0.5000\%$ drift gate.


* **9. Cutover & Audit Agents (`CutoverAgent` & `AuditAgent`)**: Executes T+0 trade re-routing and generates immutable SHA-256 compliance evidence packs.



---

## 📑 7-Step Migration Lifecycle

1. **Step 1: Firm Knowledge Studio** — Read-Only Wealth Kernel Panel + Interactive Acquisition Discovery Form.


2. **Step 2: Dashboard Overview** — Dynamically calculated advisor wave cohort schedules (Pilot, Wave 1, Wave 2, Wave 3).


3. **Step 3: Entity Scope Selector** — Interactive Scope Menu Card (`IN_SCOPE`, `PHASE_2`, `ARCHIVE_ONLY`, `OUT_OF_SCOPE`).


4. **Step 4: Entity Sample Files** — Targeted CSV upload portal rendered for in-scope entities.


5. **Step 5: Agent Pipeline Studio** — Master execution conductor with 3-pane slide-out Inspector Drawer (Raw Source | Editable SQL | Target Output).


6. **Step 6: HITL Approval Queue** — Human-in-the-Loop review queue for Rep-as-PM sleeve math invariants and atomic cutover sign-off.


7. **Step 7: Reconciliation & Drift** — Financial control total audit comparing source and target market values.



---

## 📁 Repository Directory Structure

```text
wealth-migration-platform/
├── agents/                     # Master 9-agent pipeline conductor
│   ├── discovery/              # Extractor, Schema Mapper, AI Foundry Mapper
│   ├── logic/                  # Sleeve Math, Compliance, Data Quality
│   ├── operational/            # Loader, Reconciler, Cutover, Audit
│   └── orchestrator.py         # 9-agent Master Orchestrator
├── config/                     # System configuration & logging
├── control_plane/
│   ├── compiler/               # Executable rulebook & expression compiler
│   │   ├── expressions.py      # Safe Expression Evaluator
│   │   ├── inference.py        # Knowledge Inference Engine
│   │   └── rulebook.py         # Markdown Rulebook Compiler
│   ├── kernel/                 # Fixed Wealth Kernel taxonomy (wealth_kernel.py)
│   ├── registry/               # Crosswalks & asset inventories
│   ├── db.py                   # SQLite Control Plane database & DDL
│   └── services/               # ADF client & control services (adf_client.py)
├── data_plane/
│   ├── adapters/               # Target adapters (SPARC, Orion)
│   ├── connectors/             # Source connectors (Amplify, Black Diamond)
│   ├── core/                   # Sleeve math engine (sleeve_engine.py)
│   ├── storage/                # Local extract storage & sample CSVs
│   ├── validation/             # Reconciliation engine (reconciler.py)
│   └── storage_manager.py      # Hybrid storage manager
├── knowledge/                  # Knowledge base JSON/Markdown assets
├── scripts/                    # Mock data generator & test helper scripts
├── tests/                      # Pytest unit & integration test suite
├── ui/
│   ├── app.py                  # FastAPI controller & REST API endpoints
│   ├── auth.py                 # Session authentication & RBAC manager
│   ├── models.py               # Pydantic request & response schemas
│   ├── state.py                # WorkspaceStateStore reactive engine
│   ├── static/                 # CSS & JavaScript engine
│   └── templates/              # Jinja2 HTML templates (XODUS-AMP Light Theme)
├── pytest.ini                  # Pytest configuration
├── requirements.txt            # Python dependencies
└── README.md                   # Platform documentation

```

---

## 🛠️ Quickstart Guide

### 1. Installation & Environment Setup

Ensure Python 3.11+ is installed, then install platform dependencies:

```bash
pip install -r requirements.txt

```

### 2. Generate Mock Test Data

Generate sample extracts (`amplify_sleeves.csv`, `amplify_positions.csv`, `blackdiamond_performance.csv`):

```bash
python scripts/generate_mock_data.py

```

### 3. Run Automated Test Suite

Execute unit and integration tests across sleeve math, reconciliation, and agent orchestration:

```bash
python -m pytest

```

*Expected output: All 9 unit tests pass in under 1 second with zero warnings.*

### 4. Launch the Control Plane Command Center

Start the FastAPI server:

```bash
python -m uvicorn ui.app:app --reload --port 8000

```

Open `[http://127.0.0.1:8000](http://127.0.0.1:8000)` in your web browser.

---

## 🔐 Credentials & Pre-Seeded User Personas

| Role | Email | Password | Scope & Permissions |
| --- | --- | --- | --- |
| **Platform Admin** | `jeeva@simplicitywealth.com` | `password123` | Full system control, overrides, and cutover sign-off

 |
| **Migration Analyst** | `analyst@simplicitywealth.com` | `password123` | Scope configuration, schema mapping, and sample uploads

 |
| **Domain SME** | `sme@simplicitywealth.com` | `password123` | Sleeve math validation and HITL cutover sign-off

 |
| **Validation Specialist** | `specialist@simplicitywealth.com` | `password123` | Post-load AUM drift audit and reconciliation sign-off

 |

```

```