# VOCABULARY.md — Common Behave Vocabulary (ASP-1617) — DRAFT FOR REVIEW

**Status:** Phase A review artifact — nothing here is implemented yet. The handler (`vocab.py`) is
built next session, *after* this vocabulary is signed off.
**Sources of truth:**
- behave terminology: <https://behave.readthedocs.io/en/latest/gherkin/> (canonical for keyword
  semantics; summarized in §1).
- Current step phrases: `../v2 Tooling/poc-pythonbdd` — `bdd_poc/scenario.py`,
  `features/steps/contractor_steps.py`, `features/contractor_onboarding.feature`,
  `features/contractor_safezone_dev.feature` (ASP-1563 PoC; read-only reference — ASP-1618, the
  refactor of those files, is Cancelled).
- Analyst terminology: the SafeZone QA tickets the PoC scenarios trace to — SAF-186, SAF-187,
  SAF-235, SAF-326, SAF-327 (richest: 18 structured ACs), SAF-328, SAF-473, SAF-489, SAF-524.

**How to review:** §4 is the decision surface — each row proposes one canonical phrase. Mark rows
agree/change. §5 states the recommendations that shaped the proposals; §6 lists the open questions
that need an explicit call.

---

## 1. Behave terminology primer (the fixed ground rules)

Per the behave Gherkin docs, these are the terms the vocabulary must be expressed in:

| Term | behave semantics |
|---|---|
| **Feature** | Top-level container; name + optional free-text description. |
| **Background** | Steps run **before every Scenario** in the feature; must precede all scenarios; keep short (docs suggest < 4 lines). |
| **Scenario** | One test case; good practice = one behaviour per scenario. |
| **Scenario Outline** + **Examples** | Parameterized scenario; runs once per Examples row; placeholders use `<name>` syntax (also substituted inside step tables/text). |
| **Given / When / Then / And / But** | The five step keywords. behave treats them **identically** at run time but the docs prescribe the semantic split: *Given* = put the system in a known state (precondition), *When* = the key action, *Then* = observe outcomes, tied to business value. `And`/`But` continue the previous kind. |
| **Step data** | Multiline text (`"""…"""` → `context.text`) and tables (`\| … \|` → `context.table`; surrounding whitespace trimmed). |
| **Tags** | `@tag` on Feature/Scenario/Outline; feature tags are inherited by scenarios; filterable (`--tags`). |
| **Step parameters** | Step definitions bind via parse expressions — `{name}` placeholders in the decorated phrase. |
| **NOT supported** | The Gherkin 6 **`Rule`** keyword. The vocabulary must never rely on it. behave *does* allow lowercase keywords and other spoken languages, but we standardise on capitalized English keywords. |

Implication used throughout §4: **"inspecting a table" is not a When.** In behave semantics the
inspection is the *observation mechanism* of a Then; the When is the action that changed the system
(the integration run). Several QA ACs phrase inspection as a When — the mapping reclassifies these.

---

## 2. Inventory — the 9 current PoC step phrases (the baseline)

| # | Kind | Current phrase | Semantic (`ScenarioContext`) |
|---|------|---------------|------------------------------|
| 1 | Given | `the SafeZone reference data is loaded` | `seed_reference(fixture, table)` — seed a reference/lookup table |
| 2 | Given | `a new contractor is added with details "{fixture}"` | `add_entity` — seed 1 mole row from a fixture; **derives the chain source** from the entity word |
| 3 | Given | `contractors are added with details "{fixture}"` | `add_entities` — seed many mole rows from a `{"rows":[…]}` fixture |
| 4 | Given | `the following Beakon contractors are seeded in dev` + data table | `add_contractor_rows` — analyst's data table merged over a template fixture; idempotent (feature-scoped seed-once) |
| 5 | When | `the integration processes are run` | `request_run()` — sets a flag only |
| 6 | When | `the SafeZone integration runs` | `request_run()` — same semantic, different wording (live-dev feature) |
| 7 | Then | `the table "{table}" has a row with` + vertical `\| column \| value \|` table | `assert_table_row` — chain resolves **lazily** here (target derived from the table name); ASP-383 `count(*)=0=pass` form |
| 8 | Then | `the table "{table}" has no row with` + table | `assert_table_no_row` — absence assertion (rejection/exclusion cases) |
| 9 | Then | `the auto-derived process chain ends at the gotcha terminal "{terminal}"` | `assert_terminal` — chain-shape assertion |

**Design invariant carried into the vocabulary** (poc CLAUDE.md, "the one idea that must not be
broken"): the **When names zero processes**. Source comes from the Given, target from the first Then;
chain resolution is lazy. No canonical phrase may hand-list processes.

---

## 3. Analyst terminology observed in the QA tickets

Recurring phrasing across the SAF stories (SAF-327 quoted as the exemplar — its 18 ACs are the most
structured Given/When/Then set we have):

| Analyst phrasing | Where | What it really is (behave terms) |
|---|---|---|
| "Given: I have the description of the table from the Implementation Guide Doc-003…" | AC-01, AC-02, AC-03 | Provenance/context, not a runnable precondition → feature description / tag, not a step |
| "Given: A new Student or Supplier record **is created** in the relevant source entity" | AC-16a | Given (seed) |
| "Given: An existing … record **is updated** / **marked as deleted** (`ind='D'`)" | AC-16b/16c | Given (mutate seeded data) — **no current step exists** |
| "When: **I inspect the table in AWS Athena**" | AC-01…AC-13, AC-17, AC-18 | **A Then in disguise** — inspection is the observation, not the action |
| "When: **I run the relevant process** to populate/refresh the entity" | AC-16a/b/c | When (the true action) — matches PoC #5/#6 |
| "Then: **I expect** the new user record **to appear** in the entity" | AC-16a | Then, row-presence (PoC #7) |
| "Then: I expect **no duplicates** in the primary key column `uon_user_id`" | AC-02, AC-18 | Then, column uniqueness — **no current step** |
| "Then: I expect **no NULLs** in the primary key column" | AC-02, AC-17, AC-18 | Then, column non-null — **no current step** |
| "Then: I expect `uon_user_id` **to be derived as** `concat('c', stu.student_id)`" | AC-05/06, AC-11 | Then, transformation check — expressible as row-value assertions (PoC #7) on chosen examples |
| "Then: I expect **only rows satisfying** `ind <> 'D'` … **to be included** / deleted rows **to be excluded**" | AC-04, AC-16c | Then, absence (PoC #8) on excluded examples |
| "Exception categories … business logic per column" (table of rules) | SAF-524, SAF-235 | Then against the exception-report output |
| "sample records covering the below combinations … end-to-end" | AC-14 | Scenario Outline + Examples |

Register clash: the tickets are first-person ("I inspect", "I expect"); the PoC steps are declarative
("the table … has a row with"). behave is agnostic; a single register must be picked (§5.1).

Synonym clashes: *entity* / *table*; *record* / *row*; *process* / *integration*; *created* /
*added* / *seeded*.

---

## 4. The mapping — proposed canonical vocabulary

Conventions used in the proposals: declarative register (§5.1); parse-expression parameters in
`{braces}`; the vertical `| column | value |` data-table convention from the PoC is retained for all
row-matching steps; environment (dev/test/uat) is **config, not vocabulary** (§5.6).

### 4.1 Given — arrange / seed

| ID | QA phrasing (ref) | Current PoC phrase | **Proposed canonical phrase** | Action id | Notes |
|---|---|---|---|---|---|
| G1 | (implicit reference-data preconditions) | #1 `the SafeZone reference data is loaded` | `the {system} reference data is loaded` | `seed_reference` | "SafeZone" becomes a parameter |
| G2 | "A new … record is created in the relevant source entity" (SAF-327 AC-16a) | #2 `a new contractor is added with details "{fixture}"` | `a new {entity} is added with details "{fixture}"` | `seed_entity` | `{entity}` is the chain-source anchor word (derivation unchanged) |
| G3 | (multi-record seed; SAF-473 setup) | #3 `contractors are added with details "{fixture}"` | `{entity} records are added with details "{fixture}"` | `seed_entities` | avoids naive pluralisation; "records" per QA register |
| G4 | "the following … are seeded" (live-dev Background) | #4 `the following Beakon contractors are seeded in dev` + table | `the following {entity} records are seeded` + data table | `seed_rows` | **drops `in dev`** — env comes from config (§5.6); template-fixture merge + idempotent seed-once semantics retained |
| G5 | "An existing … record is updated …" (AC-16b) | — (gap) | `the {entity} record with {column} "{value}" is updated with` + data table | `update_seeded_row` | new capability: mutate a seeded mole row, so update-flow CRUD ACs are runnable |
| G6 | "An existing … record is marked as deleted (`ind='D'`)" (AC-16c) | — (gap) | `the {entity} record with {column} "{value}" is marked deleted` | `delete_seeded_row` | sets the CDC delete indicator on the seeded row |
| G7 | (harness checkpoint — ASP-1614/1616, not in QA tickets) | — (C1 CLI only) | `a watermark checkpoint is recorded for tables "{tables}"` | `record_watermarks` | binds C1 `discover_watermarks()`; `{tables}` = comma-separated `db.table` list; checkpoint stored in the scenario context (serializable — chains to G8/T7/T8) |

### 4.2 When — the action

| ID | QA phrasing (ref) | Current PoC phrase | **Proposed canonical phrase** | Action id | Notes |
|---|---|---|---|---|---|
| W1 | "I run the relevant process to populate/refresh the entity" (SAF-327 AC-16) | #5 `the integration processes are run` / #6 `the SafeZone integration runs` | `the {target} integration runs` | `request_run` | collapses the two current Whens into one parameterized phrase; `{target}` is a friendly system word ("SafeZone"), **never a process list** — the zero-process invariant holds (chain still derives lazily from Given + first Then) |
| — | "**I inspect the table in AWS Athena**" (AC-01 onwards) | — | **no canonical When** | — | reclassified: inspection is the observation mechanism of every Then (§1). The mapping guide (this doc) is the translation instruction for HU/AI converting ACs |

### 4.3 Then — observe / assert

| ID | QA phrasing (ref) | Current PoC phrase | **Proposed canonical phrase** | Action id | Notes |
|---|---|---|---|---|---|
| T1 | "I expect the new … record to appear in the entity" (AC-16a); per-field expectations (AC-03/05/06/09/10/11/12/13) | #7 `the table "{table}" has a row with` + table | `the table "{table}" has a row with` + `\| column \| value \|` table | `assert_row_present` | unchanged — already the workhorse; transformation/derivation ACs are asserted **example-based** through it (§5.4); first Then still anchors chain derivation |
| T2 | "deleted rows … to be excluded"; "that record no longer contributes" (AC-04, AC-16c, SAF-473 exclusion) | #8 `the table "{table}" has no row with` + table | `the table "{table}" has no row with` + table | `assert_row_absent` | unchanged |
| T3 | (chain-shape check — PoC-specific, no QA phrasing) | #9 `the auto-derived process chain ends at the gotcha terminal "{terminal}"` | `the auto-derived process chain ends at the terminal "{terminal}"` | `assert_terminal` | **drops "gotcha"** — PoC jargon (§5.5) |
| T4 | "I expect **no duplicates** in the primary key column `uon_user_id`" (AC-02, AC-18) | — (gap) | `the column "{column}" in table "{table}" has no duplicates` | `assert_column_unique` | direct AC-02 fit; composite keys via comma-list in `{column}` |
| T5 | "I expect **no NULLs** in the primary key column" (AC-02, AC-17, AC-18) | — (gap) | `the column "{column}" in table "{table}" has no nulls` | `assert_column_not_null` | |
| T6 | row-count / "one record per normalized email" (AC-02, AC-08) | — (gap) | `the table "{table}" has exactly {count:d} rows matching` + table (table optional → whole-table count) | `assert_row_count` | covers "exactly one winning record" ACs; `{count:d}`=0 duplicates T2 — lint should flag that (Phase B) |
| T7 | (harness invariant — ASP-1616) | — | `the watermark for "{table}" is unchanged` | `assert_watermark_unchanged` | re-runs C1 for `{table}` and compares to the G7 checkpoint — the `output_1a == output_1b` invariant as a step |
| T8 | exception-report membership (SAF-235 / SAF-524 categories) | — (gap) | *(no new phrase)* — use T1/T2 against the exception-report table | — | the exception report **is a table**; a dedicated phrase would be sugar (§6 Q3 if disagreement) |

### 4.4 Cleanup / teardown — the C2 rollback

| ID | QA phrasing (ref) | Current | **Proposed canonical phrase** | Action id | Notes |
|---|---|---|---|---|---|
| C1 | (harness teardown — ASP-1615) | — (C2 CLI only) | `the tables are rolled back to the watermark checkpoint` | `rollback_to_checkpoint` | binds C2 `rollback_aud(..., apply=True)` against the G7 checkpoint. All C2 guardrails carry over verbatim: PK required per table (no PK → refused per-table, never fatal), snapshot-before recorded, `_aud` truncate stays out. Keyword: written as a final `Then`/`And` in explicit scenarios (e.g. ASP-1616 as Gherkin); the *implicit* form (an `@rollback`-tagged `after_scenario` hook) is a Phase B design decision (§6 Q4) |

### 4.5 Structure-level mappings (not steps)

| QA construct | Canonical Gherkin construct |
|---|---|
| "I have the description … from Doc-003" (AC-01 provenance) | Feature/Scenario **description text** + a doc link comment — not a runnable step |
| AC ↔ scenario traceability | **Tags**: `@SAF-327 @AC-02` on the scenario (behave inherits feature tags; filterable with `--tags`) |
| AC-14 "sample records covering the below combinations" | **Scenario Outline + Examples** — one Examples row per combination |
| Shared seeding for a whole feature (live-dev seed-once) | **Background** (G4 + W1), exactly as `contractor_safezone_dev.feature` does today |
| Grouping rules (Gherkin `Rule`) | **Never used** — unsupported by behave; group with tags/features instead |

---

## 5. Recommendations (the calls embedded in §4 — please confirm)

1. **Declarative register, not first-person.** Canonical steps read as system facts
   (`the table … has a row with`), not analyst actions (`I expect …`). behave doesn't mandate either;
   declarative parses cleanly, matches the existing 9 phrases, and reads identically in Background
   (where "I" is ambiguous). The mapping table is the translation guide for HU/AI converting
   first-person ACs.
2. **"When I inspect …" maps to Then.** The QA tickets' inspection-Whens are observations; the only
   true When in this domain is the integration run. This keeps the PoC's lazy-chain model intact.
3. **One When.** `the {target} integration runs` replaces both current wordings. The parameter is a
   system word, never a process list (the ASP-1563 invariant).
4. **Transformation ACs are example-based, not rule-based.** AC-05 "derived as
   `concat('c', stu.student_id)`" is asserted by seeding a known example and asserting the derived
   value via T1 — not by embedding SQL expressions in step text (leaky, unparseable, untestable
   offline). Rule-based checks stay in DQDL/exception tickets. (Dissent → §6 Q2.)
5. **Synonym policy: one canonical word each, synonyms resolved by this document** (not by accepting
   N phrasings in parse patterns — permissive matching breeds drift). Canon: **row** (physical,
   in Then), **record** (in Given seeding phrases, matching QA register), **table** (physical Then
   target), **entity** (the business object word in Given, which doubles as the chain-source anchor),
   **integration** (not "process(es)") in the When. "gotcha" is dropped (T3).
6. **Environment is config, not vocabulary.** G4 drops `in dev`; env selection stays with the
   backend/config layer (`ENV_CODE` / `WATERMARK_ENV`), so the same feature file runs in dev/test/uat
   unchanged — consistent with how C1/C2 already treat env.
7. **Traceability via tags** (`@SAF-nnn @AC-nn`), inherited per behave semantics — replaces the
   comment-based provenance sprinkled through the PoC features (comments stay welcome as prose).

---

## 6. Open questions for review

1. **Schema/metadata ACs (SAF-327 AC-01) — in scope for the vocabulary?** "Column names and
   constraints match the documentation" needs a machine-readable Doc-003 schema to be runnable. A
   phrase like `the table "{table}" matches its documented schema` is easy to reserve now,
   implementable only when a schema source exists. Reserve it, or leave metadata ACs manual?
2. **Rule-based assertions** — is an escape-hatch phrase like
   `every row in table "{table}" satisfies "{condition}"` (raw SQL predicate) wanted, despite §5.4?
   Powerful for analysts who write SQL; leaky as vocabulary.
3. **Exception-report sugar** — accept T8's "just use T1/T2 on the report table", or is a dedicated
   analyst-friendly phrase (`the exception report flags …`) worth the extra vocabulary?
4. **Rollback ergonomics** — explicit step (C1) only, or also an `@rollback` tag + `after_scenario`
   hook so ordinary scenarios get auto-teardown without spelling it? (Hook = Phase B
   `environment.py` design; the step form is needed either way to express ASP-1616 as Gherkin.)
5. **`{entity} records are added …` (G3)** — happy with this fix for the pluralisation problem, or
   prefer keeping bare plurals (`contractors are added …`) as an accepted alias?
6. **Composite keys in T4** (comma-list in one `{column}` parameter) — acceptable, or should
   composite uniqueness be its own phrase?

---

## 7. What happens after sign-off (Phase B, next session — for context only)

The signed-off §4 becomes the canonical registry in a new `vocab.py` in this repo (serializable
`VocabularyEntry`/`Vocabulary` dataclasses per repo conventions; generic behave binding via
`register_steps`; offline `--lint`/`--dump` CLI; offline tests). poc-pythonbdd is not modified
(ASP-1618 Cancelled).
