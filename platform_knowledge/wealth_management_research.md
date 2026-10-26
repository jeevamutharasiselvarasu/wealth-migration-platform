# XODUS Platform Knowledge — Wealth Management Firms, RIA Acquisitions, and the Account / Client / Household Entities

Status: research draft v1 (26 Oct 2026). Platform-provided knowledge only. It must stay separate from migration demo data (CoreCap / Simplicity samples).
Scope: Account, Client, Household. No Party entity. Source-to-canonical direction only.

Confidence tags: **[S]** = stated in a fetched vendor/industry source (listed at the end). **[G]** = general industry knowledge that was not verified in this research pass and should be reviewed by the Domain SME before it drives any rule.

---

## 1. How a wealth-management firm (RIA) works

**1.1 What an RIA is.** A Registered Investment Adviser is a firm registered with the SEC or a state that gives investment advice for a fee. It owes clients a fiduciary duty [G]. Its public disclosure is Form ADV: Part 1 holds structured firm data (AUM, ownership, custody), and Part 2A is the narrative brochure on fees, conflicts, and services [S, partly: Form ADV accuracy vs. actual practice is a diligence item].

**1.2 People and roles.** Typical roles: lead advisers who own client relationships, associate/service advisers, paraplanners, operations staff, a trading desk, and compliance. Individual advisers who deal with clients are Investment Adviser Representatives (IARs) whose registrations (Form U4) sit with the sponsoring firm [S].

**1.3 Revenue model.** Most RIAs charge a percentage-of-AUM fee, billed quarterly or monthly, in advance or arrears. Fee schedules are often tiered with breakpoints and may aggregate across related accounts [G]. Fees are commonly deducted directly from client accounts, which is itself a custody authority that regulators scrutinise [S: "direct fee deduction" is a custody diligence item].

**1.4 Technology stack (what holds which data).**

| Layer | Purpose | Typical systems [G, examples named in sources] |
|---|---|---|
| Custodian | Holds the assets; source of truth for positions, balances, transactions | Schwab Advisor Services, Fidelity, Pershing, TradePMR, Altruist |
| Portfolio accounting / reporting | Performance, billing, client reports | Orion, Addepar, Black Diamond, Tamarac |
| CRM | Contacts, relationships, notes, service workflow | Salesforce FSC, Redtail, Wealthbox, Junxure |
| Planning | Financial plans | MoneyGuide, eMoney |
| Compliance / archiving | Books and records | various |

Custodians publish daily close-of-business files covering positions, balances, transactions, pricing and security data, cost basis, and client/account profile information; firms pick which files and delivery method to use [S: Schwab daily data files]. Custodian files are the usual feed into portfolio accounting; CRM usually receives household/account data from the portfolio-accounting system [S: Orion→FSC example].

**1.5 Implication for XODUS.** "Source" data from an acquired firm is almost never one clean table. It is an export from a portfolio-accounting system and/or CRM, with custodian-derived account identifiers. File profiling and entity classification must expect partial exports, mixed grain (account-level vs. client-level rows), and inconsistent identifiers.

---

## 2. How RIA acquisitions work and where data moves

**2.1 Deal structures and why they matter for data.**
- Asset purchase: client advisory contracts are assigned directly to the buyer.
- Stock/membership purchase: a controlling block (generally 25% or more of voting interests) is treated as an indirect assignment by operation of law.
- Merger: any change of control triggers assignment [S].

**2.2 Client consent.** Under Advisers Act Section 205(a)(2) a change of control is an assignment of advisory contracts, so client consent is a closing condition. Buyers check each contract for **negative consent** (clients are presumed to consent unless they object) or **affirmative consent**. The mix drives timing and attrition risk, and attrition is typically handled through retention-based price adjustments or earnouts [S].

*Data consequence:* the acquired book is not final until consents are known. A client record may need a consent status (consented / negative-consent period running / objected / pending), and the migration should be able to hold back or flag clients and their accounts that have not consented. [G — design implication, not stated in sources]

**2.3 Due-diligence data that later becomes migration scope** [S]: Form ADV accuracy; contract assignability and consent provisions; custody arrangements (qualified custodian, direct fee deduction); examination and enforcement history; revenue concentration (large clients, referral sources, wrap-fee sponsors); books and records completeness and retrievability (Rule 204-2, including email archives).

**2.4 Closing and post-closing regulatory steps** [S]: a non-registered buyer completes its own SEC/state registration before or at closing; dual registrants may need a FINRA Rule 1017 application; IAR Form U4 amendments through CRD (generally within 30 days); state notice filings confirmed; an already-registered buyer promptly amends Form ADV for changes in AUM, control persons, and business description. Record-keeping obligations transfer with the client relationships, so record access is a closing deliverable.

**2.5 Operational integration — the data conversion itself.**
- Inventory both firms' systems, decide which to adopt and which to retire, assign an owner to each key system, and train by role [S].
- Using the same CRM at both firms made one merger easier; disparate CRMs and differing data standards create silos [S].
- CRM consolidation best practice: map fields across both systems, find redundancies, clean data (remove outdated contacts and duplicates, verify regulatory notes and portfolio records) **before** moving it, standardise formats such as names and dates, pilot with a limited dataset, move lower-risk data first then high-priority client data, and monitor progress [S].
- Avoid keeping legacy tools "just in case"; delayed consolidation causes client confusion, internal miscommunication, and lost revenue [S].

**2.6 Custodian transition (often runs alongside the data conversion).** Advisers retrieve all relevant client data (demographics and performance history) from the old custodian or provider, keep an internal copy, and port it to the new one. Some custodians restrict access by time horizon or data type, or export only in certain formats. Clients typically complete new paperwork. Pitfalls named: not bringing over all client data, unclear timelines, and a provider that cannot support the transition [S].
*Unverified in this pass [G]:* ACAT transfer mechanics, re-papering documents, billing cut-over, cost-basis continuity, and performance-history reconciliation. These need a dedicated follow-up before XODUS encodes any reconciliation rule.

**2.7 Typical failure modes to design against.** Duplicate people across firms; households that differ in definition (see §3); accounts missing owners; joint accounts mapped to one owner only; identifiers that do not survive the move (system IDs vs. custodian account numbers); lost performance/cost-basis history; clients who have not consented being migrated. [G, consistent with the sourced pitfalls above]

---

## 3. The three entities

### 3.1 Account
**Definition.** A single financial account such as an IRA or brokerage account, imported from the portfolio-management system [S: Tamarac]. In Salesforce FSC it is a *Financial Account* object, fed from Orion on a schedule [S].
**Core facts that matter for migration.**
- Identity: custodian account number plus the system's own account ID. Orion's FSC integration uses its own Source System ID formats for households and persons; the source did not define a Financial Account matching key [S].
- Registration/type decides who owns it and who can act. Reference rules [S: Series 65 study material]: individual (one owner; TOD avoids probate); joint WROS (equal owners, survivor takes all); joint TIC (set percentages, no survivorship); community property and tenancy by entirety (married couples only; divorce converts TBE to tenancy in common); custodial UGMA/UTMA (minor is beneficial owner, one custodian, one minor, taxed under the minor's SSN); discretionary (power of attorney to the adviser); guardianship; POA features (limited vs. full, durable vs. non-durable). Trust, corporate and IRA registrations were **not** covered by that source [G for those].
- An account relates to groups: one account can be in many reporting groups but at most one rebalancing group in Tamarac; billing groups total accounts that share billing settings [S].
- Investment data (positions, holdings, performance) belongs to the portfolio-accounting system; in FSC, holdings are read-only [S]. XODUS's POC scope covers the Account entity, not holdings.

### 3.2 Client
**Definition varies by system — this is a key mapping risk.**
- Tamarac: a client is a **point of contact identified by a unique email address**; it need not own an account (can be a CPA) and can have client-portal access. Each client belongs to only one household. Client↔account is many-to-many (joint accounts have several clients) [S].
- Salesforce FSC: individuals are modelled as Person Accounts or Contacts; membership of a group is via Account-Contact Relationships. Person Accounts cannot sit in an account hierarchy, which households work around; a person has one Primary Group, which affects roll-ups [S].
- Addepar: clients and households are *entities* alongside accounts and trusts, with a `model_type`; ownership between entities is a separate relationship [S, partial].
- Orion hierarchy: Firm → Representative → Household → Registration → Account → Asset → Transaction. Households contain members; **Registration** sits between household and account [S].
**Migration implications.** "Client" may mean legal owner, relationship contact, portal user, or primary decision-maker depending on the source. XODUS's canonical Client should be defined explicitly in `canonical_contract.md`, and each source's meaning recorded in mapping notes. Dedup keys (email, tax-ID surrogate, name+DOB) are SME decisions; do not store real tax IDs in demo or knowledge files. Entities that are not people (trusts, companies) are a known gap because Party is out of scope — they should be captured as Client with a type attribute or flagged as an SME change request. [G — design implication]

### 3.3 Household
**Definition.** A collection of relationships that share a common goal; it combines accounts, groups, clients, and client portals; it controls what clients see in the portal; the address can be maintained at household level and synced down; it can be tied to service teams and planning integrations [S: Tamarac]. In FSC a Household is an Account record type giving a collective view of related people and businesses [S].
**Relationship rules [S unless noted].**
- Tamarac: client belongs to one household; groups can nest inside households; dynamic reports run on groups, not households (except net-worth and plan-summary reports); the source did not say whether an account can be in more than one household or billing group.
- FSC: individuals are direct members, businesses are indirect members; one primary group per person.
- Orion: related households can be set for **billing breakpoints** (title of source; details not retrieved) — households can matter for fee tiers.
**Households are not the same as billing or reporting groups.** Billing groups "often mirror" households but need not [S]. Firms define households differently (family unit, advisory relationship, billing unit) [G]. During an acquisition the two firms' household definitions rarely match, and this is the main semantic mapping decision for the Household entity.

### 3.4 Relationship summary (reference for the canonical model)

```
Household 1 ──< Client (client belongs to one household; Tamarac, FSC primary group)
Client  >──< Account (many-to-many; joint accounts have several owners)
Household 1 ──< Account (via owners/groups; accounts can also sit in extra reporting groups)
Account  >── Billing group / Reporting group / Rebalancing group (overlay constructs, not the household)
```
Whether Household→Account is direct or derived from owners is a canonical-model decision to confirm with the SME. [G]

---

## 4. Platform rules this suggests (proposals for SME review, not yet decisions)

1. Keep a `source_system_id` plus `custodian_account_number` for every account; never rely on one alone. Orion's ID-format and ordering pitfalls (create persons before households or you get duplicates) show identifier handling must be explicit [S].
2. A household's definition must be recorded per source (family / advisory relationship / billing unit) in mapping notes.
3. Joint and multi-owner accounts must keep all owners and ownership percentage where the registration is TIC; a single-owner flattening is a reviewable mapping warning. [G + S]
4. Clients without accounts (e.g., third-party contacts) are valid in Tamarac; the target model must say whether they migrate. [S]
5. Consent status gates which clients and accounts can be treated as migrated, when the deal needs negative or affirmative consent. [S + G]
6. Clean before moving; pilot a slice first; migrate lower-risk data before high-priority client data. This matches XODUS's human-review and wave-evaluation flow, but wave assignment is out of POC scope. [S]
7. Reconciliation checks the Validator can run deterministically: counts of clients, households and accounts by source vs. target; accounts with no owner; clients with no household; households with no accounts; duplicate identifiers. [G]
8. Billing, performance history, cost basis, holdings, and consent are real acquisition concerns but outside the three-entity POC; record them as known out-of-scope so the demo narrative is honest.

---

## 5. Gaps and follow-ups

- Not verified: ACAT/re-papering mechanics, billing cut-over, cost-basis and performance-history continuity, Regulation S-P/privacy handling (the buyer's-guide source did not address it), Orion Registration object details, Addepar ownership-graph details, FSC Financial Account roles, trust/corporate/IRA registration rules.
- Several sources are vendor marketing; the Series 65 study page is educational material, not a regulation. Legal points (Section 205, Rule 204-2, Rule 1017) should be reviewed by compliance counsel before being cited externally.
- Suggested next research step: confirm the CoreCap-style export shapes the demo should imitate (client list, household roll-up, account list with registration and owners).

## Sources
- Tamarac, Introduction to Accounts, Groups, Households and Clients: https://help.tamaracinc.com/help/content/tamarac_reporting/accounts-groups-households/introduction_to_accounts_groups_households_and_clients.htm
- Salesforce data models, Groups and Household: https://developer.salesforce.com/docs/platform/data-models/guide/groups-and-household.html
- Coforge, Salesforce FSC modelling relationships part 1: https://blog.coforge.com/blog/salesforce-fsc-modelling-relationships-part-1
- Vantage Point, FSC + Orion integration guide: https://vantagepoint.io/blog/sf/how-to-integrate-salesforce-financial-services-cloud-with-orion-advisor-tech-the-complete-implementation-guide
- Addepar developer docs, Entities: https://developers.addepar.com/docs/entities
- Acquisition Stars, RIA M&A buyer's guide: https://acquisitionstars.com/blog/buying-an-ria-firm-legal-guide
- SS&C Black Diamond, CRM consolidation for RIA M&A: https://www.sscblackdiamond.com/discover/blog/streamlining-success-best-practices-for-crm-consolidation-with-ria-mergers-and-acquisitions
- InvestmentNews, Merging RIAs, merging technology: https://investmentnews.com/fintech/merging-rias-merging-technology/56658
- TradePMR, preparing for an RIA custodian transition: https://tradepmr.com/blog/three-steps-to-prepare-for-an-ria-custodian-transition
- Schwab Advisor Services, daily data files: https://advisorservices.schwab.com/managing-your-business/tech-integration/daily-data-files
- Achievable, Series 65 account ownership and registration types: https://app.achievable.me/study/finra-series-65/learn/recommendations-and-strategies-account-ownership-brokerage-account-types
