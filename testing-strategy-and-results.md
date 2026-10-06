# Testing (Assignment Section 5) — Implementation & Results

Status as of 2026-10-01, commits `ac30145`, `a48f6d0`.

## Why this was needed

Assignment Section 5 ("Testing") requires two specific Rasa mechanisms:
- **Rasa NLU Testing**: `rasa test nlu`, confusion-matrix analysis, cross-validation.
- **Dialogue Testing**: `rasa test core`, story transitions, edge-case/fallback handling, extended test stories covering request variations and sustainability preferences.

Neither existed in this project before this work. The existing `tests/*.py` pytest suite is custom Python unit/integration testing of `actions/*.py` — valuable, but not Rasa's own NLU/Core testing mechanisms, and doesn't satisfy this section.

**Hard constraint from the user**: test data must never overlap with training data. This is enforced mechanically, not just by convention — see below.

## What was built

| File | Purpose |
|---|---|
| `tests/test_nlu.yml` | Held-out NLU test set — fresh phrasing/cities/currencies for every trainable intent, never copied from `data/nlu.yml` |
| `tests/test_nlu_no_overlap.py` | pytest that loads both NLU files and fails the build if any held-out example exactly matches a training example (normalised, entity-markup stripped) |
| `tests/test_stories.yml` | Rasa test-story format for `rasa test core` — happy paths, 3 sustainability-level variants, content drift, task switching, fallback escalation |
| `tests/test_form_required_slots.py` | Direct unit test for the adaptive `priority_focus` branch in `ValidateTripPlanningForm.required_slots()` — a gap `rasa test core` can't reach (it only evaluates policy predictions, never a form's own Python branching) |

Run commands (see README.md "Testing" section for the full writeup):
```bash
pytest tests/ -v
rasa test nlu -u tests/test_nlu.yml -m models/<model>.tar.gz --out results/nlu_held_out
rasa test nlu --cross-validation --folds 5 -u data/nlu.yml --out results/nlu_cross_validation
rasa test core --stories tests/test_stories.yml -m models/<model>.tar.gz --out results/core
rasa data validate --data data/ --domain domain.yml --config config.yml
```

## Key design decision: test stories must start mid-loop

`rasa test core` only evaluates **policy predictions** — it never executes real action code. This project has three places where a custom action hands off to the next step via a runtime `FollowupAction` rather than a trained rule: `action_route_entry_point`, `action_generate_trip_recommendations` → `book_confirmation_form`, and `action_handle_task_switch`. `data/stories.yml`'s own header comment already explains these can't be trained as static rule transitions (causes `InvalidRule` errors). The same thing turned out to be true for **test** stories: a first attempt asserting these hand-offs directly failed every such story with a reproduced `predicted: action_listen` mismatch (confirmed via a real `rasa test core` run, not assumed). Fix: short "entry routing" stories test only the learnable half of each hand-off; every other story tests a form's internal behaviour by starting with `active_loop: <form>` already set — confirmed 100% accurate once restructured this way.

## Real results

**Core/dialogue**: 100% conversation accuracy, 100% action-prediction accuracy, 32/32 test stories, 0 warnings.

**NLU — held-out set** (268 fresh examples): **73.9%** intent accuracy.

**NLU — 5-fold cross-validation** (directly on `data/nlu.yml`, the assignment's own prescribed method): **99.7%** train accuracy vs **68.3% (±3.4%)** test accuracy. Entity extraction: 99.4% train F1 vs **45.9%** test F1.

Two independently-constructed methods converge on ~68–74% — this is a real, corroborated finding: **the NLU model overfits the exact training phrasings** and doesn't generalise as well as its near-100% training accuracy suggests.

Nuance: splitting the 70 held-out-set misses by the FallbackClassifier's own 0.65 confidence threshold (`config.yml`) shows 43/70 are *low-confidence* misses that `nlu_fallback` already catches and routes to the smart-fallback recovery flow in production — so practical end-to-end robustness is better than the raw 73.9% suggests. The remaining 27 are *confident-but-wrong* misclassifications, concentrated in:
- Semantically adjacent intent trios that already share training examples by design: `ask_trip_planning` / `ask_trip_to_this_city` / `change_destination_city`.
- `inform_sustainability_level` vs `inform_priority_focus`.
- `deny` vs `deny_booking`.
- The smallest intents overall: `greet`, `affirm`, `deny`, `goodbye`, `request_human_handover` (5–10 training examples each).

Per-intent breakdown (held-out set) confirms the same pattern via precision/recall: worst recall is `deny` (0.20), `request_human_handover` (0.40), `goodbye` (0.40), `inform_sustainability_level` (0.43) — all small-example intents. Per-entity: `destination_city`/`origin_city` show low recall (0.38/0.76) in DIET's own tagging, but this is **not** practically concerning — those two slots are filled entirely through a custom regex extractor (`extract_destination_city`/`extract_origin_city` in `actions.py`, `type: custom` in domain.yml) that never consults this entity tag at all. `sustainability_level`, `priority_focus`, `city_name` and the currency entities (0.34–0.50 recall) *do* rely directly on DIET's `from_entity` mappings, so those numbers are practically meaningful.

**Suggested next step, if pursued**: add more diverse training examples to exactly those intents (not a pipeline/config change) — same approach already used successfully for `ask_trip_planning`/`ask_trip_to_this_city`/`change_destination_city` in earlier work this engagement.

**pytest**: 190/192 passing. The 2 failures (`test_city_matching.py::TestStructuredButtonPayloadFullExtractionPipeline`) are pre-existing and unrelated — confirmed via `git stash` to fail identically on the unmodified codebase.

**`rasa data validate`** (story/rule structure-conflict check, the assignment's own data-consistency angle on top of runtime testing): "No story structure conflicts found" at both default and `--max-history 8` (matching `TEDPolicy`'s own setting). It does warn about several intents/utterances "not used in any story or rule" — these are expected false positives for a form-heavy project (e.g. `affirm`, `deny`, `confirm_booking`, every `inform_*` intent, every `utter_ask_<slot>` response): the validator doesn't know forms absorb these generically through domain.yml's slot mappings rather than through an explicit story/rule step.

## Why Core's 100% is expected, not a leakage red flag (user raised this, worth keeping)

Unlike an ML classifier, 100% here isn't suspicious: `results/core/TEDPolicy_report.json` is literally `{}` — of 167 action predictions across the 32 test stories, **zero** were answered by `TEDPolicy` (the only trainable/statistical component in the Core pipeline). Every single one came from `RulePolicy` deterministically replaying `data/rules.yml`. So this suite checks "does deployed behaviour match the hand-written specification", the same kind of check the `pytest` suite already does for `actions/*.py` — not "does a model generalise to unseen input," which is what the NLU numbers above honestly measure (and don't get 100% on). A genuinely stronger Core test would need multi-edge-case chained scenarios that no single rule covers end-to-end, forcing `TEDPolicy` to actually arbitrate — a real possible next step, not yet done.

## Open items noticed while reading the assignment PDF (not yet actioned)

Surfaced for completeness, not yet verified by direct code inspection, not part of this task:
- Assignment names Climatiq API (carbon) and Amadeus sandbox API (hotels/flights) specifically — not yet confirmed whether `actions/api_clients.py` / `data_services/` use these exact providers.
- Assignment suggests DIETClassifier + spaCy tokenizer — `config.yml` actually uses `WhitespaceTokenizer` (confirmed directly this round, directly relevant to the user's own question about whether DIETClassifier is used — it is, just not with the spaCy tokenizer the assignment suggests).
- Assignment describes a dedicated `action_default_fallback` + two-stage clarification flow — this project uses a custom `action_smart_fallback` with a 3-strike streak system instead (documented in the user's own architecture notes, so likely an intentional deviation).
- Assignment requires a colour-coded (green/amber/red) results card driven by a carbon score "returned from the Climatiq API" — not yet verified against the actual trip-results card implementation.
- Task 3 (Figma/Miro/Lucidchart/Rasa Webchat flow visualization) has not been done at any point in this project.
