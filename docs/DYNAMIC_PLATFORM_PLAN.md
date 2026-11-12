# Making the platform dynamic: audit and plan (2026-11-12)

Goal: nothing about a particular source, target or firm is written in code. A new source or target system, with its own files and its
business-rules markdown, must work by configuration and inference alone. No LLM is available, so inference is deterministic, and a small
rule language (DSL) carries what people write down.

## 1. Where the platform is hard-coded today (audit)

| Area | What is fixed in code or tracked files | Effect of a different target or source |
|---|---|---|
| Entity names | `P0_ENTITIES` and `CORE_ENTITIES` (`control_plane/services/firm_config.py`), `ENTITIES` in `target_inference.py`, `SUPPORTING_ENTITIES` (added this session) | A target table that is not Account, Client or Household is never offered; the list never changes with the target. |
| Target to entity step | `target_inference.infer` only tries the three fixed entities, by name and field similarity to the canonical model | Other tables are "unassigned". |
| Canonical model | `config/firm_config.yaml`, `platform_knowledge/canonical_model_v2.yaml` (5 entities, wealth fields such as HH_ID, REG_TYPE) | The canonical vocabulary is wealth-specific and fixed. |
| Vocabularies | `platform_knowledge/field_vocabulary.yaml`, `source_vocabulary.yaml`, `knowledge/entity_inference_rules.json`, `amplify_dictionary.json`, `source_profile.json`, `knowledge/firm/*` | Source and target field recognition is tuned to CoreCap, Amplify and Simplicity. |
| Built-in rule families | `CLIENT_TYPE_LOOKUP`, `CUSTODIAN_LOOKUP`, `REG_TYPE_LOOKUP`, `PREFIX_FORMAT` (`xodus_pipelines.py`, `transformation_rules.py`) | Domain value maps live in code, outside the business-rules file. |
| Entity resolution | `SUGGESTED_RULES` (`entity_resolution.py`) | Match rules are fixed per entity name. |
| Demo data | `demo_data/` (source files for three firms, manifest, knowledge, users), `/api/demo/load`, `/api/blob/seed-demo`, first-start seed, `XODUS_SEED_DEMO`, 42 test files that call the loader | Every restart can recreate a demo migration; tests depend on it. |
| Target database | The Azure "Target database" setting points to database `XODUS`, schema `simplicity` (the platform's own database). The real `Simplicity` database, schema `simplicity`, already exists with the 3 Orion tables and is reachable; only the live-target setting uses it. | The target model is inferred from the wrong database. |

## 2. What is already data-driven

- The target model is read from the target database catalog (tables and columns), then confirmed by a person.
- The business-rules markdown is inferred into governed rules, and "Target <field> comes from <Entity> <Field>" statements propose
  target mappings (added this session).
- Source files are classified from their headers and values against the vocabularies above.

## 3. Plan (in order; each phase leaves the suite green)

**Phase 0: correct the target database (configuration, no code).** As Platform Admin: set Target database to `Simplicity`, schema
`simplicity`; read the schema; infer the target model; confirm it; adopt a new target adapter. The XODUS database stays the platform
database and the mirror.

**Phase 1: remove the demo data.** Delete `demo_data/` source, knowledge and manifest files, the demo load and seed routes and buttons,
`XODUS_SEED_DEMO`. Tests get a small synthetic `tests/fixtures/` set and a test-only helper that drives the real setup and upload
endpoints. Sign-in accounts are not demo data and are kept (moved out of `demo_data/`, not read).

**Phase 2: entities from the target.** The scope list is the confirmed target's tables grouped into entities, with no fixed names.
`infer` assigns tables to entities from the catalog (table-name concepts, surrogate-key families, shared columns) without a fixed
tuple. Supporting data is derived from relationships the canonical model and the business rules declare, not from a list in code.

**Phase 3: a rule language for everything people write.** Extend the statement grammar to the full set the specs use: conditions
("where HouseholdOrder = 1"), joins, concatenation with fallbacks, value maps with target-list validation, defaults, and
target-field-from-source statements. The built-in domain families move into the business-rules file as ordinary statements. Entity-resolution
match rules become statements too.

**Phase 4: canonical model and vocabularies as inputs.** Treat the canonical model and vocabularies as versioned platform knowledge that a
Platform Admin edits (and the removal of knowledge assets already lets a migration swap its own), not as code-tracked wealth definitions.
Prove it by running a second, unrelated source and target (the existing Ledgerline fixture) end to end with no code change.

Acceptance for each phase: change the target or the source in the UI (or in the markdown), and the entity list, mappings and rules change with it,
with no code edit.

## 5. Status after phases 0 to 4 (2026-11-13)

Done and covered by tests:
- Target database is `Simplicity` / `simplicity`; scope choices come from the confirmed target; supporting data comes from canonical key names.
- No demo data in the platform; no fixed entity names; domain value maps and by-field-name transforms are out of the code.
- Rule language: conditions (`where Field = value`), `COPY`, mapping statements.
- Knowledge locations are settings (`XODUS_FIRM_CONFIG`, `XODUS_PLATFORM_KNOWLEDGE_DIR`, `XODUS_KNOWLEDGE_DIR`); entity header terms are a data file
  (`platform_knowledge/entity_terms.yaml`) and, for an entity with none, come from its own definition; the source vocabulary reaches classification.
- Proof: `tests/test_second_domain.py` runs a public library (members, books, loans) in a fresh process with only its own configuration,
  target schema, vocabulary, source files and rules file: entities, target-table matching, an unmatched table, file classification, rule inference,
  mapping statements, supporting entities and SQL generation all work with no code change and no wealth words in any output.

Not yet generic (still wealth-shaped code, found by grepping for HH_ID, ACCT_NUM, ADVISOR_ID, Household, AccountRole, custodian):
- `agents/discovery/schema_mapper_agent.py` (`DEFAULT_CATALOG` source-name aliases, canonical field checks, about 34 references)
- `control_plane/services/entity_resolution.py` (`SUGGESTED_RULES` per entity, household-head derivation), `canonical_derivations.py`
- `control_plane/services/mcl_validation.py` and `migration_validation.py` (Account to Household to Client relationship checks)
- `control_plane/services/sql_package.py` (`KEY_FIELDS` set of identifier names used to choose MERGE keys)
- `control_plane/services/xodus_pipelines.py`, `adf_exporter.py` (legacy), `control_plane/kernel/wealth_kernel.py`, `agents/p0_agent_suite.py`
- about 20 places in `ui/app.py`

These do not stop a second domain from being configured and inferred (the proof above), but running a second domain through source-to-canonical
mapping, validation and the generated pipelines needs them moved to configuration, one module at a time, each proven by extending the library test.
