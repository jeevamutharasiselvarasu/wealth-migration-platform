# Platform walk, 2026-11-19

What was walked, what was found, what was fixed, and what could not be walked here. Written after building the last items of the wave, restart and knowledge work (phases 1 to 7 and their follow-ups).

## How it was walked

- A throwaway server on http://127.0.0.1:8765 started from a launcher script. It runs in a temporary data folder, so the real `App_Data` was never opened, and it holds pre-made test sessions for each of the six roles (Platform Admin, Migration Analyst, Validation Specialist, IMO Lead, Domain SME, Compliance Reviewer), so no password was typed or stored anywhere. The sample migration from `tests/fixtures` (CoreCap to Simplicity, 9 source files) was loaded the way a person would set it up.
- Three passes over every page that takes no parameters (49 pages) as each of the six roles, 294 requests per pass: before any migration existed, with the sample migration loaded, and after the analyst confirmed the 9 files, generated and approved 20 mapping proposals and the validator ran mapping validation. Every response was checked for server errors, template errors and tracebacks.
- A visual walk in the in-app browser, signed in as the role that owns each page: Firm knowledge (search, Helpful marks, tuning and decision-model panels), Platform knowledge (Knowledge sources as the Platform Admin), Schema Mapper (evidence under the mapping suggestions), Business rules (evidence under the rule suggestions), Target validation (the validation-rules section, a rule saved as a draft through the form, and the dry-run error).
- Knowledge features were exercised with real files: an Excel workbook (two sheets), a Markdown rules file, an approved web host and Confluence settings.

## Result of the crawl

No page returned a server error or showed a template error in any of the three states, for any role. Redirects and 403s were the intended ones: the Platform Admin cannot open migration data, Compliance Reviewer and Domain SME have no enabled migration action, and pages that need an active migration or a package say so. The two 404/409 answers for the analyst roles (`create-scripts`, `transformation-sql`) are the correct "nothing approved to build from yet" answers.

## Findings, all fixed

| # | Where | Finding | Fix |
|---|---|---|---|
| W1 | Firm knowledge, search | Marking a result Helpful or Not helpful reloaded the page without the search, so the next result needed a new search. | The search is kept in the redirect and the page returns to the search panel. |
| W2 | Platform knowledge, Knowledge sources | The Confluence and web-host fields had only placeholders; once filled in, a reader could not tell the allowed spaces from the token variable. | Every field has a visible label. |
| W3 | Schema Mapper, Business rules, Discovery | Evidence appeared under almost every suggestion, many of them filler matches: the query contained words such as "field", "canonical", "value map" that occur in every document. | Queries now contain only the distinguishing words (entity, field name, canonical field); each embedder has its own evidence threshold (0.2 for the built-in one) that `XODUS_EVIDENCE_MIN_SCORE` can override. Evidence under the mapping suggestions fell from 18 to 13 panels of 25 rows. |
| W4 | Firm knowledge | The search card added in phase 7 sat inside the grid and the page's collapsible-section wrapper (`<details>`) swallowed its `lg:col-span-3` class, squeezing it into one narrow column. | The card is outside the grid and full width. Note for any future card: a card with a grid span class inside that grid loses the span, because the wrapper becomes the grid child. |
| W5 | Target validation, rules form | Entity, layer, severity and name were bare dropdowns and a placeholder. | Labelled fields. |
| W6 | Tests | The phase 6 web-link form added a fourth form section to Firm knowledge and an older UI boundary test counted three. | The test counts four and says which. |

Not a finding but worth knowing: templates are cached by the running server, so a template change shows only after a restart (this is how it already behaved).

## Not walked, and why

- **The data plane against Azure** (stages 07 to 10 with the stable-pipeline package): there is no Azure, Data Factory, SQL Server or Blob storage in this sandbox. The wave board, entity rerun, wave gate, validation findings with failing keys, waivers, the dry run of a rule and promotion are covered by unit tests of the generated SQL and pipelines and of the application routes, but not by a real run. They are the first thing to run in the factory.
- **Confluence and web links against the real services**: tested with simulated answers only. The connection test, space limits and page cap need a real Confluence.
- **A semantic embedding model**: none was installed. Search and evidence use the built-in vocabulary-matching embedder, so "owner" does not find "household" until the model is installed (`fastembed`, see `Dockerfile.control_plane`) or the retrieval tuning has learned it from reviewers' marks.
- **Retrieval tuning and the decision model on real data**: they need 30 Helpful marks and 60 decisions; the walk had one mark and 20 decisions. Both refused to train and said how much was missing, as designed.

## Still open

- The two tests in `tests/test_p0_migration_scope_selection.py` that fail on the commit before phase 1 (a template text check and a platform-knowledge file check).
- Failing keys carry the source file and row only when the key is the canonical key; keys of mirror-only columns stay untraced.
- Tuning the embedding model's own weights and a classifier for entity classification were not built: the first needs a GPU-class training stack, the second has too little per-migration data to beat the existing rules.
