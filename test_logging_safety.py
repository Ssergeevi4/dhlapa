from pathlib import Path
import logging

import pytest

from config.logging import PIIFilter, mask_sensitive_text, mask_sensitive_value
from log_safety import find_log_template_violations


pytestmark = pytest.mark.no_db


def test_log_masking_redacts_email_phone_and_medical_fields():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "email=client@example.com phone=+7 913 123-45-67 "
            "diagnoses=raw medical text"
        ),
        args=(),
        exc_info=None,
    )

    PIIFilter().filter(record)

    assert "client@example.com" not in record.msg
    assert "+7 913 123-45-67" not in record.msg
    assert "raw medical text" not in record.msg
    assert "[REDACTED_EMAIL]" in record.msg
    assert "[REDACTED_PHONE]" in record.msg
    assert "[REDACTED_MEDICAL]" in record.msg


def test_log_masking_redacts_sensitive_dict_args():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="client payload %(payload)s",
        args=(
            {
                "payload": {
                    "phone": "+79131234567",
                    "email": "client@example.com",
                    "diagnoses": "raw medical text",
                    "safe": "visible",
                }
            },
        ),
        exc_info=None,
    )

    PIIFilter().filter(record)

    assert record.args["payload"]["phone"] == "[REDACTED_PHONE]"
    assert record.args["payload"]["email"] == "[REDACTED_EMAIL]"
    assert record.args["payload"]["diagnoses"] == "[REDACTED_MEDICAL]"
    assert record.args["payload"]["safe"] == "visible"


def test_mask_helpers_share_unified_format():
    assert mask_sensitive_text("Call +79131234567") == "Call [REDACTED_PHONE]"
    assert mask_sensitive_value("a@b.example", key="email") == "[REDACTED_EMAIL]"
    assert mask_sensitive_value("diagnosis text", key="diagnoses") == "[REDACTED_MEDICAL]"


def test_log_template_checker_flags_sensitive_variables(tmp_path: Path):
    unsafe = tmp_path / "unsafe.py"
    unsafe.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def run(phone):\n"
        "    logger.info('phone=%s', phone)\n",
        encoding="utf-8",
    )

    violations = find_log_template_violations([unsafe])

    assert len(violations) == 1
    assert violations[0].line == 4


def test_log_template_checker_allows_safe_templates(tmp_path: Path):
    safe = tmp_path / "safe.py"
    safe.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def run(user_id):\n"
        "    logger.info('user_updated user_id=%s', user_id)\n",
        encoding="utf-8",
    )

    assert find_log_template_violations([safe]) == []
