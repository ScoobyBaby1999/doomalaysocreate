import pytest

from hypothesis.models import RequirementCandidate


@pytest.fixture
def make_req():
    """Factory for RequirementCandidate. Tests override only what varies."""
    def _make_req(**kwargs: object) -> RequirementCandidate:
        defaults: dict[str, object] = {
            "id": "REQ-001",
            "system_name": "AuthService",
            "system_response": "authenticate the user",
        }
        return RequirementCandidate(**(defaults | kwargs))
    return _make_req
