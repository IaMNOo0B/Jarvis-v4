"""Vision: captures are code, answers are OBSERVATIONS (never
facts), and every failure is an honest error — no key, no capture,
no invented screen."""

import base64

import jarvis.browser_cdp as cdp
import jarvis.vision as vision
from jarvis.chat import ChatApp


def test_desktop_capture_is_honest_off_windows():
    png, err = vision.capture_desktop_png()
    assert png is None and "Windows" in err     # this VM is Linux


def test_analyze_requires_a_key():
    out = vision.analyze_image("", b"\x89PNG fake")
    assert out["ok"] is False and "/key" in out["error"]


def test_analyze_shapes_an_observation(monkeypatch):
    class _Provider:
        def __init__(self, key):
            self.key = key

        def generate_content(self, payload):
            parts = payload["contents"][0]["parts"]
            assert parts[1]["inlineData"]["mimeType"] == "image/png"
            return {"candidates": [{"content": {"parts": [
                {"text": "Ek code editor khula hai."}]}}]}

    monkeypatch.setattr("jarvis.providers.GeminiProvider", _Provider)
    out = vision.analyze_image("key", b"\x89PNG fake", "kya hai?")
    assert out["ok"] is True
    assert "editor" in out["observation"]
    assert "observation" in out["note"]


def test_browser_screenshot_op(monkeypatch):
    class _Session:
        def call(self, method, params=None):
            assert method == "Page.captureScreenshot"
            return {"data": base64.b64encode(b"png!").decode()}

    monkeypatch.setattr(cdp, "_ensure_session", lambda: _Session())
    monkeypatch.setattr(cdp, "_refresh_current", lambda s: None)
    out = cdp.op_browser_screenshot()
    assert out["ok"] is True and out["png_b64"]


def test_app_advertises_vision_tools(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert {"see_screen", "see_page", "browser_screenshot"} <= names
    out = app.execute_tool("see_screen", {})
    assert out["ok"] is False          # Linux VM: honest refusal
    assert "Windows" in out["error"]
