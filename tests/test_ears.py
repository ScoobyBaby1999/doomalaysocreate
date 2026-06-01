"""
Tests for hypothesis/ears.py.

All tests are purely deterministic — no API keys, no network calls.
Run with: pytest -v tests/test_ears.py
"""

import pytest

from hypothesis.ears import (
    ERR_INVALID_PATTERN,
    ERR_SYSTEM_NAME,
    ERR_SYSTEM_RESPONSE,
    apply,
    classify,
    synthesize,
    validate,
)
from hypothesis.models import EarsPattern, RequirementCandidate


# ---------------------------------------------------------------------------
# Pattern classification and canonical sentence tests
# ---------------------------------------------------------------------------

def test_ubiquitous(make_req):
    req = apply(make_req())
    assert req.ears_pattern is EarsPattern.UBIQUITOUS
    assert req.ears_valid is True
    assert req.text == "The AuthService shall authenticate the user."


def test_event(make_req):
    req = apply(make_req(trigger="the user submits the login form"))
    assert req.ears_pattern is EarsPattern.EVENT
    assert req.ears_valid is True
    assert req.text == "When the user submits the login form, the AuthService shall authenticate the user."


def test_state(make_req):
    req = apply(make_req(state="the system is in maintenance mode"))
    assert req.ears_pattern is EarsPattern.STATE
    assert req.ears_valid is True
    assert req.text == "While the system is in maintenance mode, the AuthService shall authenticate the user."


def test_unwanted(make_req):
    req = apply(make_req(precondition="authentication fails three times"))
    assert req.ears_pattern is EarsPattern.UNWANTED
    assert req.ears_valid is True
    # EARS canonical form requires "then" for the UNWANTED pattern (sole precondition)
    assert "then" in req.text
    assert req.text == "If authentication fails three times, then the AuthService shall authenticate the user."


def test_optional(make_req):
    req = apply(make_req(feature="two-factor authentication is enabled"))
    assert req.ears_pattern is EarsPattern.OPTIONAL
    assert req.ears_valid is True
    assert req.text == "Where two-factor authentication is enabled, the AuthService shall authenticate the user."


# ---------------------------------------------------------------------------
# COMPLEX pattern
# ---------------------------------------------------------------------------

def test_complex_state_trigger(make_req):
    req = apply(make_req(
        state="the user session is active",
        trigger="the session token expires",
    ))
    assert req.ears_pattern is EarsPattern.COMPLEX
    assert req.ears_valid is True
    # Clause order: state before trigger (CLAUSE_SLOTS canonical order)
    expected = (
        "While the user session is active, when the session token expires, "
        "the AuthService shall authenticate the user."
    )
    assert req.text == expected


def test_complex_three_clauses(make_req):
    req = apply(make_req(
        precondition="the user account is not locked",
        state="the system is online",
        trigger="the user submits credentials",
    ))
    assert req.ears_pattern is EarsPattern.COMPLEX
    # ', then ' must NOT appear in COMPLEX (only in sole-precondition UNWANTED).
    # Note: bare 'then' can appear inside words like 'authenticate', so we check
    # the specific UNWANTED construct ', then '.
    assert ", then " not in req.text
    # Clause order: precondition → state → trigger
    assert req.text.startswith("If the user account is not locked,")
    assert "while the system is online," in req.text
    assert "when the user submits credentials," in req.text


def test_complex_all_four_clauses(make_req):
    req = apply(make_req(
        precondition="the license is valid",
        state="the system is idle",
        feature="auto-lock is enabled",
        trigger="the idle timer fires",
    ))
    assert req.ears_pattern is EarsPattern.COMPLEX
    assert req.ears_valid is True
    # All four clause keywords present in order (search lowercased — first char is uppercased)
    text = req.text.lower()
    assert text.index("if ") < text.index("while ") < text.index("where ") < text.index("when ")


def test_complex_exact_clause_order(make_req):
    """Assert full string for a three-clause COMPLEX to lock in ordering."""
    req = apply(make_req(
        system_name="PaymentService",
        system_response="process the payment",
        precondition="the account balance is sufficient",
        state="the payment gateway is reachable",
        trigger="the user confirms checkout",
    ))
    expected = (
        "If the account balance is sufficient, "
        "while the payment gateway is reachable, "
        "when the user confirms checkout, "
        "the PaymentService shall process the payment."
    )
    assert req.text == expected


# ---------------------------------------------------------------------------
# INVALID pattern
# ---------------------------------------------------------------------------

def test_invalid_no_system_name(make_req):
    req = apply(make_req(system_name=None))
    assert req.ears_pattern is EarsPattern.INVALID
    assert req.ears_valid is False
    assert ERR_SYSTEM_NAME in validate(make_req(system_name=None))


def test_invalid_no_response(make_req):
    req = apply(make_req(system_response=None))
    assert req.ears_pattern is EarsPattern.INVALID
    assert req.ears_valid is False
    assert ERR_SYSTEM_RESPONSE in validate(make_req(system_response=None))


def test_invalid_returns_empty_text(make_req):
    req = apply(make_req(system_name=None))
    assert req.text == ""
    assert synthesize(make_req(system_name=None)) == ""


# ---------------------------------------------------------------------------
# apply() contract
# ---------------------------------------------------------------------------

def test_apply_sets_all_three_fields(make_req):
    raw = make_req()
    result = apply(raw)
    assert result.ears_pattern is not None
    assert isinstance(result.ears_valid, bool)
    assert isinstance(result.text, str)


def test_apply_returns_new_instance(make_req):
    raw = make_req()
    result = apply(raw)
    assert result is not raw


def test_idempotence(make_req):
    """apply(apply(req)) must equal apply(req) on all three fields it sets."""
    raw = make_req(trigger="the user clicks submit")
    once = apply(raw)
    twice = apply(once)
    assert twice.ears_pattern is once.ears_pattern
    assert twice.ears_valid == once.ears_valid
    assert twice.text == once.text


def test_determinism(make_req):
    """Identical input -> identical output, every time."""
    raw = make_req(state="the server is running")
    r1 = apply(raw)
    r2 = apply(raw)
    assert r1.ears_pattern is r2.ears_pattern
    assert r1.ears_valid == r2.ears_valid
    assert r1.text == r2.text


# ---------------------------------------------------------------------------
# Whitespace / empty normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("val", ["", " ", "\t", "\n", "  \t  "])
def test_whitespace_trigger_treated_as_absent(make_req, val):
    """Whitespace-only trigger is treated as absent → UBIQUITOUS, not EVENT."""
    req = apply(make_req(trigger=val))
    assert req.ears_pattern is EarsPattern.UBIQUITOUS


def test_whitespace_system_name_is_invalid(make_req):
    """Whitespace-only system_name is normalized to None → INVALID.
    Closes the gap where _ears_invariant passed '  ' via Python truthiness."""
    req = apply(make_req(system_name="   "))
    assert req.ears_pattern is EarsPattern.INVALID
    assert req.ears_valid is False


def test_validate_valid_requirement_returns_empty_list(make_req):
    errors = validate(make_req())
    assert errors == []


# ---------------------------------------------------------------------------
# Error constant stability
# ---------------------------------------------------------------------------

def test_error_constants_match_validate_output(make_req):
    """validate() error strings must equal the named ERR_* constants exactly."""
    errors_no_name = validate(make_req(system_name=None))
    errors_no_resp = validate(make_req(system_response=None))
    assert ERR_SYSTEM_NAME in errors_no_name
    assert ERR_SYSTEM_RESPONSE in errors_no_resp


# ---------------------------------------------------------------------------
# Casing preservation
# ---------------------------------------------------------------------------

def test_casing_preserved_system_name(make_req):
    """system_name casing must not be altered."""
    req = apply(make_req(system_name="OAuth"))
    assert "OAuth" in req.text
    assert "oauth" not in req.text


def test_casing_preserved_feature(make_req):
    """Feature clause content must not be lowercased."""
    req = apply(make_req(feature="GitHub Actions integration is configured"))
    assert "GitHub Actions" in req.text


def test_acronym_system_name_preserved(make_req):
    """All-caps acronym system names must not be mangled."""
    req = apply(make_req(system_name="NASA"))
    assert "NASA" in req.text
    assert "nasa" not in req.text


# ---------------------------------------------------------------------------
# Double-article regression
# ---------------------------------------------------------------------------

def test_no_double_article_ubiquitous(make_req):
    """system_name starting with 'The' must not produce 'The The ...'"""
    req = apply(make_req(system_name="The Payment System"))
    assert "The The" not in req.text
    assert req.text.startswith("The Payment System shall")


def test_no_double_article_event(make_req):
    """Double-article bug in EVENT template with 'The'-prefixed system name."""
    req = apply(make_req(
        system_name="The Auth System",
        trigger="the user logs in",
    ))
    assert "the The" not in req.text
    assert "The Auth System" in req.text


def test_article_added_for_plain_name(make_req):
    """system_name without leading article gets 'the' prepended (mid-sentence)."""
    req = apply(make_req(trigger="the user clicks submit"))
    assert "the AuthService shall" in req.text


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

def test_serialization_roundtrip_valid(make_req):
    """model_dump() -> model_validate() must preserve EARS fields."""
    req = apply(make_req())
    dumped = req.model_dump()
    reloaded = RequirementCandidate.model_validate(dumped)
    assert reloaded.ears_pattern is req.ears_pattern
    assert reloaded.ears_valid == req.ears_valid
    assert reloaded.text == req.text


def test_serialization_roundtrip_complex(make_req):
    """Round-trip a COMPLEX requirement."""
    req = apply(make_req(
        state="the service is degraded",
        trigger="a health-check probe arrives",
    ))
    assert req.ears_pattern is EarsPattern.COMPLEX
    dumped = req.model_dump()
    reloaded = RequirementCandidate.model_validate(dumped)
    assert reloaded.ears_pattern is EarsPattern.COMPLEX
    assert reloaded.text == req.text
