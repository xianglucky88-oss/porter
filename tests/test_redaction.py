"""脱敏与失败归因的单元测试（不依赖 mockapp）。"""

from __future__ import annotations

import pytest

from app import redaction
from app.config import Settings
from app.errors import (
    BusinessRejection,
    FailureClass,
    FlowDefinitionError,
    TransientFailure,
    code_of,
    failure_class_of,
    retryable_of,
)


@pytest.mark.parametrize(
    "raw",
    [
        "applicant=张三&phone=13800138000&token=abc123",
        "id_card: 330106199001011234",
        "uscc=91330100MA2xxxxxxx",
        "mail 李四@example.com",
        "card_no: 6222020200112233445",
    ],
)
def test_free_text_is_redacted(raw):
    out = redaction.redact_text(raw)
    assert redaction.MASK in out
    for token in ("13800138000", "330106199001011234", "91330100MA2xxxxxxx", "李四@example.com", "6222020200112233445"):
        assert token not in out


def test_redaction_is_idempotent():
    once = redaction.redact_text("phone=13800138000")
    assert redaction.redact_text(once) == once


def test_structured_values_by_key():
    data = {"applicant": "张三", "uscc": "91330100MA2XXXXXXX", "amount_cents": "100"}
    out = redaction.redact_value(data)
    assert out["uscc"] == redaction.MASK
    assert out["applicant"] == "张三"
    assert out["amount_cents"] == "100"


def test_redaction_handles_nested_structures():
    payload = {"outer": [{"password": "p@ss"}, {"phone": "13900139000"}]}
    out = redaction.redact_value(payload)
    assert out["outer"][0]["password"] == redaction.MASK
    assert out["outer"][1]["phone"] == redaction.MASK


def test_non_sensitive_text_is_untouched():
    assert redaction.redact_text("step 3 (fill): selector input#applicant") == (
        "step 3 (fill): selector input#applicant"
    )


def test_failure_taxonomy_flags():
    business = BusinessRejection("nope")
    assert failure_class_of(business) is FailureClass.BUSINESS
    assert business.retryable is False
    assert code_of(business) == "business_rejected"

    flow = FlowDefinitionError("selector missing")
    assert failure_class_of(flow) is FailureClass.FLOW
    assert flow.retryable is False

    transient = TransientFailure("timeout")
    assert transient.retryable is True


def test_unknown_exceptions_are_internal_not_business():
    exc = ZeroDivisionError("boom")
    assert failure_class_of(exc) is FailureClass.INTERNAL
    assert retryable_of(exc) is False


def test_config_rejects_invalid_boolean():
    import os

    os.environ["HEADLESS"] = "maybe"
    try:
        with pytest.raises(ValueError):
            Settings()
    finally:
        del os.environ["HEADLESS"]


def test_config_rejects_invalid_choice():
    import os

    os.environ["BROWSER_FALLBACK"] = "browser"
    try:
        with pytest.raises(ValueError):
            Settings()
    finally:
        del os.environ["BROWSER_FALLBACK"]


def test_config_parses_extra_args():
    import os

    os.environ["BROWSER_EXTRA_ARGS"] = "--foo=1 --bar 'two words'"
    try:
        assert Settings().browser_extra_args == ["--foo=1", "--bar", "two words"]
    finally:
        del os.environ["BROWSER_EXTRA_ARGS"]
