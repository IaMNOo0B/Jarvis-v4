"""The data dir lives with the USER, not the app folder —
so a fresh ZIP download stops arriving amnesiac (the
owner's keys sit in a local file inside the data dir on
his PC; an app-folder data dir wiped them every time)."""

from jarvis.chat import _resolve_data_dir
from jarvis.providers import StubProvider


def test_legacy_data_is_copied_to_the_stable_home(tmp_path):
    legacy = tmp_path / "app" / "data"
    legacy.mkdir(parents=True)
    (legacy / "secrets.json").write_text("{}")
    (legacy / "memory").mkdir()
    stable = tmp_path / "user" / "JarvisV4"
    out = _resolve_data_dir(legacy, stable)
    assert out == stable
    assert (stable / "secrets.json").exists()
    assert (stable / "memory").is_dir()
    # The legacy copy is left untouched (a copy, not a move).
    assert (legacy / "secrets.json").exists()


def test_stable_data_wins_and_legacy_is_left_alone(tmp_path):
    legacy = tmp_path / "data"
    legacy.mkdir()
    (legacy / "old.txt").write_text("purana")
    stable = tmp_path / "stable"
    stable.mkdir()
    (stable / "current.txt").write_text("naya")
    out = _resolve_data_dir(legacy, stable)
    assert out == stable
    assert not (stable / "old.txt").exists()


def test_fresh_install_lands_in_the_stable_home(tmp_path):
    out = _resolve_data_dir(tmp_path / "nope",
                            tmp_path / "stable")
    assert out == tmp_path / "stable"


def test_stub_reply_tells_the_owner_the_fix():
    reply = StubProvider().complete(
        [{"role": "user", "content": "hii"}])
    assert "offline stub provider" in reply.text
    assert "Settings" in reply.text and "Ollama" in reply.text


def test_state_word_is_polled_with_the_fx_loop():
    from jarvis.ui import PAGE
    fx = PAGE.split("async function pollFx()")[1].split("}")[0]
    assert "showState(s.activity)" in fx
