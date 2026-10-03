"""Headless Streamlit test using streamlit.testing (no server / browser needed)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "ui" / "app.py")


def test_ui_backend_down_shows_error_message():
    at = AppTest.from_file(APP, default_timeout=15).run()
    assert not at.exception
    at.chat_input[0].set_value("hello").run()
    assert not at.exception
    assert any("⚠️" in m.value for m in at.chat_message[-1].markdown)


def test_ui_happy_path():
    def fake_post(url, json=None, timeout=None):
        r = MagicMock(status_code=200)
        r.json.return_value = {
            "session_id": json["session_id"],
            "reply": "INC0010001 is In Progress.",
            "tool_calls": [{"name": "get_incident", "args": {"number": "INC0010001"}, "result": '{"found": true}'}],
        }
        return r

    with patch("requests.post", fake_post):
        at = AppTest.from_file(APP, default_timeout=15).run()
        at.chat_input[0].set_value("status of INC0010001").run()
    assert not at.exception
    texts = [m.value for cm in at.chat_message for m in cm.markdown]
    assert "INC0010001 is In Progress." in texts
