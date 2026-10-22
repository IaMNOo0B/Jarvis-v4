"""The buddy overlay's brain (its window needs the owner's
Windows desktop): the words it paints are the app's TRUE
state, decided by a pure function."""

from jarvis.overlay import buddy_text


def test_buddy_paints_the_true_state():
    line, colour, sub = buddy_text(
        {"activity": "standby", "notifications": 0,
         "provider": "gemini"})
    assert "Standby" in line and "gemini" in sub
    line, _, sub = buddy_text(
        {"activity": "creating", "notifications": 2})
    assert "Bana raha" in line and "2 nayi khabar" in sub
    line, _, _ = buddy_text(
        {"activity": "standby",
         "voice": {"active": True, "speaking": False}})
    assert "Sun raha" in line          # voice truth beats activity
    line, _, _ = buddy_text({})
    assert "Standby" in line           # unknowns rest, never fake
