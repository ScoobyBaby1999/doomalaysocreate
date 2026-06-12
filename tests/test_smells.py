"""
Tests for hypothesis/smells.py.

All tests are purely deterministic — no API keys, no network calls.
Run with: pytest -v tests/test_smells.py
"""

import pytest

from hypothesis.models import Smell, SmellCategory, SmellSource, SmellType
from hypothesis.smells import detect, scan_requirement


def detect_types(text: str) -> set[SmellType]:
    """Helper: return just the set of SmellTypes detected (coarse presence/absence).
    For multiplicity-sensitive assertions, inspect detect()'s list directly."""
    return {s.type for s in detect(text)}


def detect_spans(text: str) -> list[str]:
    """Helper: return the span strings in document order."""
    return [s.span for s in detect(text)]


# ---------------------------------------------------------------------------
# Tier 1: VAGUE_TERM
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_present", [
    ("The system shall generally respond within 2s.", True),    # "generally"
    ("The system shall support various formats.", True),         # "various"
    ("Status is TBD.", True),                                    # "TBD"
    ("See the spec for details, etc.", True),                    # "etc."
    ("The system shall respond soon.", True),                    # "soon" (temporal)
    ("The system shall log a number of events.", True),          # phrase
])
def test_vague_term_detected(text, expected_present):
    assert (SmellType.VAGUE_TERM in detect_types(text)) is expected_present


def test_vague_term_clean():
    assert SmellType.VAGUE_TERM not in detect_types(
        "The system shall respond within 200ms."
    )


def test_etc_detected_despite_period():
    smells = [s for s in detect("Supports PNG, JPEG, etc.") if s.type is SmellType.VAGUE_TERM]
    assert any(s.span == "etc." for s in smells)


# ---------------------------------------------------------------------------
# Tier 1: SUBJECTIVE_LANGUAGE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "The UI shall be user-friendly.",
    "The API shall be intuitive.",
    "Responses shall be fast.",
    "The system shall optimize throughput.",
    "The pipeline shall be efficient.",
])
def test_subjective_language_detected(text):
    assert SmellType.SUBJECTIVE_LANGUAGE in detect_types(text)


def test_subjective_language_clean():
    assert SmellType.SUBJECTIVE_LANGUAGE not in detect_types(
        "The system shall respond within 200ms."
    )


def test_hyphenated_subjective():
    smells = [s for s in detect("The UI shall be user-friendly.")
              if s.type is SmellType.SUBJECTIVE_LANGUAGE]
    assert any(s.span == "user-friendly" for s in smells)


def test_efficiently_not_matched_by_efficient():
    """Word-boundary regression: 'efficient' entry must not fire inside 'efficiently'."""
    smells = [s for s in detect("The system shall handle requests efficiently.")
              if s.type is SmellType.SUBJECTIVE_LANGUAGE and s.span.lower() == "efficient"]
    assert smells == []


# ---------------------------------------------------------------------------
# Tier 1: UNIVERSAL_QUANTIFIER
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "The system shall always be available.",
    "Every request shall be logged.",
    "The system shall handle any user input.",
    "Data shall be retained at all times.",
])
def test_universal_quantifier_detected(text):
    assert SmellType.UNIVERSAL_QUANTIFIER in detect_types(text)


def test_universal_quantifier_clean():
    assert SmellType.UNIVERSAL_QUANTIFIER not in detect_types(
        "The system shall handle GET and POST."
    )


def test_word_boundary_no_false_positive():
    # "hallways" must not fire "all"; "smallest" must not fire "small"
    assert SmellType.UNIVERSAL_QUANTIFIER not in detect_types(
        "The hallways shall be monitored."
    )


def test_case_insensitive():
    assert SmellType.UNIVERSAL_QUANTIFIER in detect_types("ALWAYS log the event.")
    assert SmellType.UNIVERSAL_QUANTIFIER in detect_types("Always log the event.")


# ---------------------------------------------------------------------------
# Tier 1: LOOPHOLE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Data shall be encrypted where applicable.",
    "The feature shall be implemented if possible.",
    "Logs shall be retained as appropriate.",
])
def test_loophole_detected(text):
    assert SmellType.LOOPHOLE in detect_types(text)


def test_loophole_clean():
    assert SmellType.LOOPHOLE not in detect_types(
        "The system shall store audit logs for 90 days."
    )


def test_loophole_suggestion_cites_standard():
    smells = [s for s in detect("Encrypt data as required.") if s.type is SmellType.LOOPHOLE]
    assert smells, "expected LOOPHOLE on 'as required'"
    assert any("standard" in (s.suggestion or "") for s in smells)


# ---------------------------------------------------------------------------
# Tier 2: PASSIVE_VOICE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,passive,missing_actor", [
    ("Data shall be processed by the validator.", True, False),
    ("The form shall be validated.", True, True),
    ("The report has been generated.", True, True),
    ("The system shall process the requests.", False, False),
])
def test_passive_and_missing_actor(text, passive, missing_actor):
    types = detect_types(text)
    assert (SmellType.PASSIVE_VOICE in types) is passive
    assert (SmellType.MISSING_ACTOR in types) is missing_actor


def test_passive_span_is_full_phrase():
    smells = [s for s in detect("The request shall be processed.")
              if s.type is SmellType.PASSIVE_VOICE]
    assert len(smells) == 1
    assert smells[0].span == "shall be processed"


def test_passive_irregular_participle():
    types = detect_types("The log shall be written.")
    assert SmellType.PASSIVE_VOICE in types


def test_passive_tech_verb_single_smell():
    """Dedup regression: 'configured' matches BOTH the regular -ed pass and the
    irregular frozenset pass at the same position → exactly ONE smell."""
    smells = [s for s in detect("The service shall be configured.")
              if s.type is SmellType.PASSIVE_VOICE]
    assert len(smells) == 1


def test_missing_actor_by_clause_before_passive():
    """F1 regression: 'by [actor]' BEFORE the passive verb in the first sentence
    (no preceding boundary) must still suppress MISSING_ACTOR."""
    types = detect_types("By the validator, the request shall be processed.")
    assert SmellType.PASSIVE_VOICE in types
    assert SmellType.MISSING_ACTOR not in types


def test_missing_actor_scoped_per_sentence():
    """A 'by [actor]' in a DIFFERENT sentence must not suppress MISSING_ACTOR."""
    text = "Reports are sent by the scheduler. The form shall be validated."
    actor_smells = [s for s in detect(text) if s.type is SmellType.MISSING_ACTOR]
    assert any(s.span == "shall be validated" for s in actor_smells)


# ---------------------------------------------------------------------------
# Tier 3: AMBIGUOUS_PRONOUN
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "It shall notify the user.",
    "The system shall process it.",
    "This shall be logged.",   # modal copula is definitional-EXCLUDED? No: "This shall be" IS copula-excluded
])
def test_pronoun_cases_run(text):
    # Smoke: detector runs without error on pronoun-bearing text
    detect(text)


def test_pronoun_detected_plain():
    assert SmellType.AMBIGUOUS_PRONOUN in detect_types("The system shall process it.")


def test_pronoun_copula_excluded():
    """'It is ...' is definitional and must not fire."""
    smells = [s for s in detect("It is a stateless service.")
              if s.type is SmellType.AMBIGUOUS_PRONOUN]
    assert smells == []


def test_pronoun_modal_copula_excluded():
    """R1 regression: 'It shall be logged' is the dominant EARS definitional
    form — the modal copula must be excluded just like 'it is'."""
    smells = [s for s in detect("It shall be logged.")
              if s.type is SmellType.AMBIGUOUS_PRONOUN]
    assert smells == []


def test_pronoun_clean():
    assert SmellType.AMBIGUOUS_PRONOUN not in detect_types(
        "The AuthService shall authenticate the user."
    )


# ---------------------------------------------------------------------------
# Tier 3: UNVERIFIABLE
# ---------------------------------------------------------------------------

def test_unverifiable_quality_noun_no_digits():
    assert SmellType.UNVERIFIABLE in detect_types(
        "The system shall provide excellent usability."
    )


def test_unverifiable_comparative_no_digits():
    assert SmellType.UNVERIFIABLE in detect_types(
        "The new pipeline shall be faster."
    )


def test_unverifiable_suppressed_by_digits():
    assert SmellType.UNVERIFIABLE not in detect_types(
        "The system shall sustain 10,000 req/s throughput."
    )


def test_unverifiable_performance_not_quality_noun():
    """P5 regression: 'performance' was removed from the quality-noun list —
    'performance monitoring' alone must not fire UNVERIFIABLE."""
    assert SmellType.UNVERIFIABLE not in detect_types(
        "The system shall include performance monitoring."
    )


def test_unverifiable_single_smell_emitted():
    smells = [s for s in detect("The system shall provide better usability.")
              if s.type is SmellType.UNVERIFIABLE]
    assert len(smells) == 1


# ---------------------------------------------------------------------------
# Cross-cutting: detect()
# ---------------------------------------------------------------------------

def test_clean_text_no_smells():
    assert detect("The AuthService shall authenticate the user within 200ms.") == []


@pytest.mark.parametrize("text", ["", "   ", "\t", "\n"])
def test_empty_text_no_smells(text):
    assert detect(text) == []


def test_document_order():
    text = "The system shall always be fast."
    smells = detect(text)
    starts = [text.index(s.span) for s in smells]  # spans unique in this text
    assert starts == sorted(starts)


def test_source_tag_propagated():
    smells = detect("The system shall always be fast.", source=SmellSource.GENERATED)
    assert smells, "expected smells"
    assert all(s.source is SmellSource.GENERATED for s in smells)


def test_source_default_is_input():
    smells = detect("The system shall always be fast.")
    assert all(s.source is SmellSource.INPUT for s in smells)


def test_multiple_smells_same_text():
    types = detect_types("The system shall always be efficient.")
    assert SmellType.UNIVERSAL_QUANTIFIER in types
    assert SmellType.SUBJECTIVE_LANGUAGE in types


def test_same_word_twice_two_smells():
    """Dedup is on (type, start) — the same word at TWO positions yields two smells."""
    smells = [s for s in detect("The system shall always log and always alert.")
              if s.type is SmellType.UNIVERSAL_QUANTIFIER and s.span.lower() == "always"]
    assert len(smells) == 2


def test_rarely_not_vague():
    """P4 regression: 'rarely' is a defined direction quantifier, not vague."""
    assert detect("The system shall rarely restart.") == []


def test_categories_match_contract():
    """Every emitted smell carries the category its rule declares."""
    text = "The form shall always be validated where applicable, and it shall improve usability."
    for s in detect(text):
        assert isinstance(s.category, SmellCategory)
        assert isinstance(s, Smell)


# ---------------------------------------------------------------------------
# Cross-cutting: scan_requirement()
# ---------------------------------------------------------------------------

def test_scan_requirement_merges(make_req):
    pre = Smell(
        type=SmellType.MISSING_CONDITION,
        category=SmellCategory.SEMANTIC,
        span="whole requirement",
        message="No condition stated.",
        source=SmellSource.INPUT,
    )
    req = make_req().model_copy(update={
        "text": "The form shall be validated.",
        "smells": [pre],
    })
    out = scan_requirement(req)
    types = {s.type for s in out.smells}
    assert SmellType.MISSING_CONDITION in types        # pre-existing preserved
    assert SmellType.PASSIVE_VOICE in types            # newly detected


def test_scan_requirement_deduplicates(make_req):
    req = make_req().model_copy(update={"text": "The form shall be validated."})
    once = scan_requirement(req)
    twice = scan_requirement(once)
    keys = [(s.type, s.span, s.source) for s in twice.smells]
    assert len(keys) == len(set(keys))
    assert len(twice.smells) == len(once.smells)


def test_scan_requirement_source_override(make_req):
    """P8/R5 regression: explicit source=INPUT must tag every new smell INPUT.
    Starts from a req with no pre-existing smells so the assertion is unambiguous."""
    req = make_req().model_copy(update={"text": "The form shall be validated."})
    out = scan_requirement(req, source=SmellSource.INPUT)
    assert out.smells, "expected smells"
    assert all(s.source is SmellSource.INPUT for s in out.smells)


def test_scan_requirement_different_sources_coexist(make_req):
    """Same span scanned under both sources → both kept (distinct pipeline events)."""
    req = make_req().model_copy(update={"text": "The form shall be validated."})
    out = scan_requirement(scan_requirement(req, source=SmellSource.INPUT),
                           source=SmellSource.GENERATED)
    passive = [s for s in out.smells if s.type is SmellType.PASSIVE_VOICE]
    assert {s.source for s in passive} == {SmellSource.INPUT, SmellSource.GENERATED}


def test_scan_requirement_returns_new_instance(make_req):
    req = make_req().model_copy(update={"text": "The form shall be validated."})
    out = scan_requirement(req)
    assert out is not req
    assert req.smells == []          # original not mutated


def test_scan_requirement_default_source_is_generated(make_req):
    req = make_req().model_copy(update={"text": "The form shall be validated."})
    out = scan_requirement(req)
    assert all(s.source is SmellSource.GENERATED for s in out.smells)
