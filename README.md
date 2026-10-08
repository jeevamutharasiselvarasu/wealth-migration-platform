```markdown
# Data Migration Workbench

A metadata-driven, agentic data migration platform built for wealth management acquisitions (specifically **Core Cap → Simplicity Wealth**). 

The platform normalizes source data (Amplify TAMP & Black Diamond) into a fixed 15-entity canonical model, enforces domain invariants (Rep-as-PM sleeve math sum to 100%, AUM drift <= 0.5%), and loads validated records into target destinations (**Salesforce SPARC** and **Orion Eclipse**).

---

## 🚀 Key Features

* **7-Step Lifecycle Workflow:**
  1. **Firm Knowledge Studio:** Interactive Discovery Questionnaire & supplementary firm document ingestion.
  2. **Dashboard Overview:** Dynamically instantiated metrics, advisor population, and wave cohort execution schedule.
  3. **Entity Scope Selector:** Interactive menu-card selector for 15 inbuilt canonical entities (`In Scope`, `Phase 2`, `Archive Only`, `Out of Scope`).
  4. **Entity Sample Files:** Targeted CSV/data file upload portal per in-scope entity.
  5. **Agent Pipeline Studio:** Visual execution conductor running 9 specialized discovery, business logic, and operational agents.
  6. **HITL Approval Queue:** Human-In-The-Loop review queue for Rep-as-PM sleeve calculations and atomic cutover authorization.
  7. **Reconciliation & Drift:** Post-load AUM variance monitoring with hard gate enforcement (<= 0.50%).

* **Role-Based Session Authentication:** Pre-configured RBAC for Platform Admins, Migration Analysts, Domain SMEs, and Validation Specialists.
* **Embedded Canonical Wealth Kernel:** Decoupled, immutable domain model covering 15 canonical entities across Master, Relationship, Event/Snapshot, and Archival categories.

---

## 📁 Repository Directory Structure

```text
wealth-migration-platform/
├── config/                     # System configuration & logging
├── control_plane/
│   ├── kernel/                 # Fixed Wealth Kernel taxonomy & rules (wealth_kernel.py)
│   ├── registry/               # Crosswalks & asset inventories
│   └── services/               # ADF client & control services (adf_client.py)
├── data_plane/
│   ├── adapters/               # Target adapters (SPARC, Orion)
│   ├── connectors/             # Source connectors (Amplify, Black Diamond)
│   ├── core/                   # Sleeve math engine & core logic
│   ├── storage/                # Local extract storage
│   └── validation/             # Reconciliation engine
├── agents/
│   ├── discovery/              # Extractor, Schema Mapper, AI Foundry Mapper
│   ├── logic/                  # Sleeve Math, Compliance, Data Quality
│   ├── operational/            # Loader, Reconciler, Cutover, Audit
│   └── orchestrator.py         # Master 9-agent pipeline conductor
├── ui/
│   ├── static/                 # CSS & JavaScript engine
│   ├── templates/              # Jinja2 HTML templates
│   ├── app.py                  # FastAPI server & route handlers
│   ├── auth.py                 # Session authentication manager
│   └── state.py                # Reactive state store (WorkspaceStateStore)
├── tests/                      # Pytest unit & integration test suite
├── scripts/                    # Helper & mock data generator scripts
├── requirements.txt            # Python dependencies
└── README.md                   # Platform documentation

```

---

## 🛠️ Quickstart Guide

### 1. Installation & Environment Setup

Ensure Python 3.11+ is installed, then install dependencies:

```bash
pip install -r requirements.txt

```

### 2. Generate Test Sample Files

Run the test data script to generate sample CSVs (`accounts.csv`, `transactions.csv`, `terminated_accounts.csv`):

```bash
python scripts/create_test_sample_files.py

```

### 3. Run Automated Tests

Execute unit and integration tests across sleeve math and reconciliation logic:

```bash
python -m pytest

```

### 4. Launch the Command Center Web UI

Start the FastAPI server:

```bash
python -m uvicorn ui.app:app --reload --port 8000

```

Open `http://localhost:8000` in your browser.

---

## 🔐 Credentials & Login Context

* **Email:** `jeeva@simplicitywealth.com`
* **Password:** `password123`
* **Roles:** `Platform Admin`, `Domain SME`, `Migration Analyst`, `Validation Specialist`

```

### Verification
Run `ls` or `dir` in your terminal at `wealth-migration-platform/` to verify that `README.md` is present alongside `requirements.txt`, `ui/`, `agents/`, `control_plane/`, and `data_plane/`.

```