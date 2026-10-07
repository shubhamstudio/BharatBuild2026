"""Regression checks for the Deputy Commissioner's reported failures."""

from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_SOURCE = (ROOT / "app" / "app.py").read_text(encoding="utf-8")
PERSONAL_DETAILS = (ROOT / "app" / "templates" / "form_step1.html").read_text(encoding="utf-8")


def test_acknowledgements_use_a_unicode_font_for_indian_language_names():
    assert "DejaVu" in APP_SOURCE


def test_deadline_is_explicitly_interpreted_in_india_standard_time():
    assert 'ZoneInfo("Asia/Kolkata")' in APP_SOURCE


def test_spouse_questions_are_gender_neutral():
    assert "Spouse's Name" in PERSONAL_DETAILS
    assert "Husband's Name" not in PERSONAL_DETAILS


def test_pending_citizens_have_a_correction_route():
    assert '"/application/<int:app_id>/edit"' in APP_SOURCE


def test_sms_gateway_can_be_disabled_for_a_real_deployment():
    assert "EXPOSE_SMS_GATEWAY" in APP_SOURCE


def test_admin_review_queue_supports_safe_search_and_filters():
    assert "application_no ILIKE %s" in APP_SOURCE
    assert "applicant_name ILIKE %s" in APP_SOURCE
    assert "applications_mobile_idx" in APP_SOURCE
