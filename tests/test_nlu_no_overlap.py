"""
tests/test_nlu_no_overlap.py — mechanically enforces the hard constraint the
user set for the Section-5 "Testing" work: tests/test_nlu.yml (the held-out
NLU test set) must NEVER contain an example that also appears in
data/nlu.yml (the training data). Without this check, someone could later
"fix" a failing held-out test by copy-pasting the offending sentence back
out of data/nlu.yml — which would silently turn the held-out set back into
a (partial) copy of the training set and make its accuracy numbers
meaningless. This test fails the whole suite the moment that happens.

Comparison is on NORMALISED text (entity-annotation markup stripped, case
and surrounding whitespace ignored) so e.g. "[Rome](destination_city)" in
one file and "Rome" in the other are still correctly caught as the same
underlying example.

Run with:  pytest tests/ -v
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

ROOT = Path(__file__).resolve().parent.parent
TRAIN_NLU = ROOT / "data" / "nlu.yml"
HELD_OUT_NLU = ROOT / "tests" / "test_nlu.yml"

# Matches Rasa's entity-annotation markup: [text](entity) or [text]{entity:value}
_ENTITY_MARKUP = re.compile(r"\[([^\]]+)\]\([^)]+\)|\[([^\]]+)\]\{[^}]+\}")


def _normalise(example: str) -> str:
    """Strip entity markup down to plain text, then fold case/whitespace."""
    def _replace(m: "re.Match") -> str:
        return m.group(1) or m.group(2) or ""

    plain = _ENTITY_MARKUP.sub(_replace, example)
    return " ".join(plain.strip().lower().split())


def _load_examples_by_intent(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    by_intent = {}
    for item in data.get("nlu", []):
        if "intent" not in item:
            continue
        intent = item["intent"]
        raw = item.get("examples", "") or ""
        lines = [
            line.strip().lstrip("-").strip()
            for line in raw.strip().split("\n")
            if line.strip()
        ]
        by_intent[intent] = {_normalise(line) for line in lines}
    return by_intent


class TestHeldOutNluDoesNotOverlapTrainingData:
    def test_held_out_file_exists_and_is_non_empty(self):
        assert HELD_OUT_NLU.exists(), f"{HELD_OUT_NLU} is missing"
        held_out = _load_examples_by_intent(HELD_OUT_NLU)
        assert held_out, "tests/test_nlu.yml has no intents/examples at all"
        total = sum(len(v) for v in held_out.values())
        assert total > 0, "tests/test_nlu.yml has intents but zero examples"

    def test_no_example_in_held_out_set_appears_in_training_data(self):
        train = _load_examples_by_intent(TRAIN_NLU)
        held_out = _load_examples_by_intent(HELD_OUT_NLU)

        all_train_examples = set()
        for examples in train.values():
            all_train_examples |= examples

        leaked = []
        for intent, examples in held_out.items():
            overlap = examples & all_train_examples
            for example in overlap:
                leaked.append(f"{intent!r}: {example!r}")

        assert not leaked, (
            "The following held-out NLU test example(s) are exact duplicates "
            "of training data in data/nlu.yml — remove or reword them so the "
            "held-out accuracy numbers stay meaningful:\n  "
            + "\n  ".join(leaked)
        )

    def test_every_trainable_intent_has_at_least_one_held_out_example(self):
        # "nlu_fallback" is the one deliberate exception — see data/nlu.yml's
        # own top-of-file comment: it's a reserved name FallbackClassifier
        # assigns automatically and is never given training examples, so it
        # can't be given held-out examples either.
        train = _load_examples_by_intent(TRAIN_NLU)
        held_out = _load_examples_by_intent(HELD_OUT_NLU)

        missing = [
            intent for intent in train
            if intent != "nlu_fallback" and not held_out.get(intent)
        ]
        assert not missing, (
            "These trainable intents have training examples but no held-out "
            f"test coverage in tests/test_nlu.yml: {missing}"
        )
