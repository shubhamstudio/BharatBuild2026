from pathlib import Path
import os

import pytest

import sys
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
import app as portal


class Cursor:
    def execute(self, *_args):
        pass

    def fetchone(self):
        return None

    def close(self):
        pass


class Connection:
    def cursor(self):
        return Cursor()

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    portal.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    monkeypatch.setattr(portal, "get_db", lambda: Connection())
    return portal.app.test_client()


def test_citizen_cannot_read_another_application(client):
    with client.session_transaction() as session:
        session["portal_mobile"] = "9000000001"
    assert client.get("/application/77").status_code == 404


def test_regular_portal_session_cannot_approve_application(client):
    with client.session_transaction() as session:
        session["logged_in"] = True
    assert client.post("/admin/approve/77").status_code == 403


def test_wrong_captcha_does_not_send_an_otp(client):
    client.get("/apply")
    response = client.post("/apply", data={"mobile": "9000000001", "captcha": "wrong"})
    assert b"security-check answer is incorrect" in response.data


def test_unicode_names_are_not_byte_truncated():
    assert portal.sanitize("\u0938\u0941\u0928\u0940\u0924\u093e \u0915\u0941\u092e\u093e\u0930") == "\u0938\u0941\u0928\u0940\u0924\u093e \u0915\u0941\u092e\u093e\u0930"


def test_scheduler_uses_the_deployed_configuration_name():
    source = (Path(__file__).parents[1] / "app" / "scripts" / "deemed_approval.py").read_text()
    assert '"config", "app.ini"' in source


def test_repository_does_not_ship_the_retired_admin_or_session_secret():
    source = (Path(__file__).parents[1] / "app" / "app.py").read_text()
    assert "sewasetu@123" not in source
    assert "ssp-prod-key-2023-donotshare" not in source


def test_requests_use_a_bounded_database_connection_pool():
    source = (Path(__file__).parents[1] / "app" / "app.py").read_text()
    assert "ThreadedConnectionPool" in source
    assert "maxconn=4" in source
