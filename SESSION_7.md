# Session 7 — ASP-1617: vocabulary review (Phase A of the common Behave vocabulary handler)

**Date:** 2026-07-03
**Ticket:** ASP-1613 → sub-task **ASP-1617** *"Introduce a common Behave vocabulary handler for
scenario-level reuse"*
**Outcome:** Phase A delivered — **`VOCABULARY.md`** (the QA-terminology ↔ PoC-step ↔ behave-keyword
mapping) written and awaiting user review. **No code written or modified** this session by design:
the user gated implementation (Phase B, `vocab.py`) behind sign-off of the vocabulary.

---

## Goal for this session

Start ASP-1617. Per the user's framing: analyse `scenario.py` and `contractor_steps.py` from
`../v2 Tooling/poc-pythonbdd` (the reusable assets the parent ticket names), pull the QA SAF stories
that PoC traced to for terminology context, and lay out the plan for the vocabulary handler — **but
review the vocabulary against current QA-ticket terminology before any implementation**, with the
behave Gherkin docs (<https://behave.readthedocs.io/en/latest/gherkin/>) as the source of truth for
behave terms.

## Research done

- **Jira:** ASP-1617 (In Progress; "shared vocabulary layer … common phrases and step semantics
  defined once at the scenario level"), parent ASP-1613 (vocabulary rationale: reuse common phrases,
  instruct HU/AI proposing scenario inputs from requirements). **Finding: ASP-1618** ("Refactor
  Behave scenario and step files to consume the shared vocabulary") **is Cancelled** → poc-pythonbdd
  is *not* modified by this work; the handler lands in this repo.
- **QA SAF stories pulled:** SAF-186, SAF-187, SAF-235, SAF-326, **SAF-327** (the exemplar — 18
  structured Given/When/Then ACs), SAF-328, SAF-473, SAF-489, SAF-524. These supply the analyst
  register: "I inspect the table in AWS Athena", "I expect no duplicates / no NULLs", "I run the
  relevant process", CRUD ACs (create/update/delete), exception-report category tables.
- **PoC assets analysed:** `bdd_poc/scenario.py` (`ScenarioContext` — framework-agnostic
  Given/When/Then logic, lazy chain resolution), `features/steps/contractor_steps.py` (9 thin behave
  bindings), both `.feature` files, `features/environment.py` (per-scenario vs feature-scoped
  seed-once lifecycles).
- **behave docs fetched:** keyword semantics (Given=precondition / When=action / Then=outcome,
  treated identically at runtime), Background-before-every-scenario, Scenario Outline+Examples,
  step tables/text, tags, parse-expression parameters, **no `Rule` keyword**.

## Decisions (user-confirmed via plan review)

| Decision | Choice |
|---|---|
| Handler location | **This BDD repo** — new flat module next session (`vocab.py`); poc-pythonbdd stays read-only (ASP-1618 Cancelled) |
| Vocabulary scope | Existing 9 PoC phrases **+ QA AC gap categories** (uniqueness, no-NULLs, transformations, CRUD update/delete, exception report, row counts) **+ C1/C2 checkpoint steps** (watermark record / rollback / watermark-unchanged) |
| Review artifact | **`VOCABULARY.md` committed in-repo** |
| Session scope | **Phase A only** — vocabulary review artifact; Phase B (implementation) deferred to next session after sign-off |

## What was produced

**`VOCABULARY.md`** — the review artifact:
- §1 behave terminology primer (the fixed ground rules, incl. the no-`Rule` constraint).
- §2 inventory of the 9 current step phrases and their `ScenarioContext` semantics, with the
  ASP-1563 invariant restated (the **When names zero processes**; chain derives from Given source +
  first Then target, lazily).
- §3 analyst terminology observed in the QA tickets, incl. the central clash: the tickets'
  "**When** I inspect the table in AWS Athena" is a **Then** in behave semantics.
- §4 the mapping table — 7 Givens (incl. new CRUD-update/delete seeds G5/G6 and the C1 watermark
  checkpoint G7), 1 When (the two current wordings collapsed into
  `the {target} integration runs`), 8 Thens (incl. new `no duplicates` / `no nulls` / row-count /
  watermark-unchanged), and the C2 rollback as an explicit cleanup step; plus structure-level
  mappings (tags for SAF/AC traceability, Scenario Outline for AC-14 sample matrices).
- §5 seven recommendations (declarative register; inspect→Then; one When; example-based
  transformation checks; one-canonical-word synonym policy; **environment is config, not
  vocabulary** — G4 drops "in dev"; tags for traceability).
- §6 six open questions for the reviewer (schema/metadata ACs, raw-SQL escape hatch,
  exception-report sugar, `@rollback` hook vs explicit step, G3 pluralisation, composite-key
  uniqueness phrasing).

## Verification

- Every §4 row carries a QA provenance ref (SAF ticket / AC number) or is marked harness-internal
  (ASP-1614/1615/1616); every proposed phrase has a behave keyword classification consistent with
  the fetched docs; all 9 baseline phrases, all 6 gap categories, and the C1/C2 steps are covered.
- No `.py` touched in either repo; no writes outside `VOCABULARY.md`, this log, and a one-line
  CLAUDE.md status update. Nothing committed (commit on request per repo convention).

## Next session (Phase B — after VOCABULARY.md sign-off)

1. Resolve §6 open questions in review; freeze the canonical registry.
2. Build `vocab.py` per the approved plan: serializable `VocabularyEntry`/`Vocabulary` dataclasses
   (repo conventions — stdlib only, JSON round-trip), generic behave binding (`register_steps`,
   behave lazily imported), action implementations delegating to a backend-agnostic context and to
   `discover_watermarks`/`rollback_aud` for the checkpoint steps, `python -m vocab --lint/--dump`
   CLI, offline tests (`tests/test_vocab.py`).
3. Prove the vocabulary expresses the real Core-4 QA scenarios: translate
   `contractor_safezone_dev.feature` to canonical phrases as a lint fixture.

Plan file (approved): `~/.claude/plans/read-claude-md-and-all-melodic-gray.md`.
