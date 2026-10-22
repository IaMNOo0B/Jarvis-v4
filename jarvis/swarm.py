"""Swarm front-end — JARVIS v4 (Sentient install, Spec 4).

Two one-call roles that give the fleet the front-end it
lacked, so "in 20 articles par yehi kaam karo" works
without the owner (or the main brain) hand-listing the
work:

- **Extractor** (one tool-free call): the owner's free-form
  "process these" request -> the discrete item list, in
  order. An empty list is an honest answer — it stops the
  pipeline instead of inventing work. When the items live
  in a file or page, its content rides along (the caller
  reads it — e.g. via the Reader — and passes it in).
- **Dispatcher** (one tool-free call): goal + items + the
  tool catalogue -> worker groups: which item indices
  share one handling, a self-contained per-item
  instruction, and the MINIMAL tool set per group. Code
  validates the plan (every index in range, every item
  assigned exactly once, every tool real); an invalid
  plan gets ONE repair round with the problems fed back,
  then fails honestly.

Execution is not here: each (group, item) pair becomes one
fleet job through the existing batch path — isolated,
gate-checked, with the fleet's honest done/failed
semantics and the Run Reporter aggregating the results.
Destructive tools inside a group still hit the gate and
the cards exactly as usual.
"""

from __future__ import annotations

import json

MAX_ITEMS = 100


def _json_from_reply(text: str):
    cleaned = str(text or "").strip()
    if "```" in cleaned:
        cleaned = cleaned.split("```")[1].strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except Exception:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception:
                return None
    return None


def _ask(brain_factory, prompt: str):
    """One guarded role call -> parsed JSON or None."""
    try:
        brain = brain_factory()
        out = brain.respond([], prompt)
        return _json_from_reply(str(out.reply))
    except Exception:
        return None


def extract_items(brain_factory, request_text: str,
                  content: str = ""):
    """The item list inside a free-form request, or None
    when the Extractor itself failed. [] means the request
    honestly names no discrete items."""
    prompt = (
        "Tum Item Extractor ho. Neeche owner ki ek "
        "'process these' wali baat hai. Us me se sirf woh "
        "alag-alag items nikalo jin par ek hi kaam baari-"
        "baari hona hai — kram (order) waisa hi rakho. "
        "Koi item mat banao jo baat me hai hi nahi.\n"
        f"BAAT: {str(request_text)[:1200]}\n"
        + (f"CONTENT (jis me items hain):\n"
           f"{str(content)[:4000]}\n" if content else "")
        + 'Reply ONLY with JSON: {"items": ["pehla item", '
          '"doosra item"]} — koi item na ho to '
          '{"items": []}')
    parsed = _ask(brain_factory, prompt)
    if not isinstance(parsed, dict) \
            or not isinstance(parsed.get("items"), list):
        return None
    items = [str(x).strip() for x in parsed["items"]
             if str(x).strip()]
    return items[:MAX_ITEMS]


def validate_groups(groups, items, tool_names) -> list:
    """The Dispatcher's plan, judged in code. Returns the
    list of problems (empty = fit to run)."""
    problems = []
    if not isinstance(groups, list) or not groups:
        return ["groups khaali hai"]
    seen = []
    for gi, group in enumerate(groups):
        if not isinstance(group, dict):
            problems.append(f"group {gi} object nahi hai")
            continue
        idxs = group.get("item_indices")
        if not isinstance(idxs, list) or not idxs:
            problems.append(f"group {gi} ke item_indices "
                            "khaali hain")
            continue
        for ix in idxs:
            if not isinstance(ix, int) \
                    or isinstance(ix, bool) \
                    or not (0 <= ix < len(items)):
                problems.append(
                    f"group {gi} ka index {ix!r} hadd se "
                    "bahar hai")
            else:
                seen.append(ix)
        if not str(group.get("instruction", "")).strip():
            problems.append(f"group {gi} ki instruction "
                            "khaali hai")
        for tool in group.get("tools") or []:
            if tool not in tool_names:
                problems.append(f"group {gi} ka tool "
                                f"'{tool}' registry me nahi "
                                "hai")
    if sorted(seen) != list(range(len(items))):
        problems.append(
            "har item bilkul ek baar assign hona chahiye "
            f"(mile: {sorted(seen)}, chahiye: "
            f"0..{len(items) - 1})")
    return problems


def dispatch_plan(brain_factory, goal: str, items,
                  tool_names):
    """Groups for the fleet, or None when the Dispatcher
    could not produce a valid plan in two rounds."""
    catalogue = ", ".join(sorted(tool_names))
    sample = "\n".join(
        f"{i}. {str(item)[:120]}"
        for i, item in enumerate(items[:12]))
    prompt = (
        f"GOAL: {str(goal)[:400]}\n"
        f"ITEMS (kul {len(items)}):\n{sample}\n"
        f"TOOLS (sirf yehi naam chal sakte hain): "
        f"{catalogue}\n"
        "Tum Dispatcher ho. Items ko groups me baanto — "
        "jin items par EK hi tareeka chalega, woh ek "
        "group. Har group ke liye: item_indices (0-based), "
        "ek SELF-CONTAINED instruction (worker sirf EK "
        "item dekhega — instruction me goal aur tareeka "
        "saaf ho, item ki jagah {item} likho), aur woh "
        "MINIMAL tools jo us group ko chahiye (send/exec "
        "wale tools tabhi jab kaam asal me bhejna ho). "
        "Har item bilkul ek group me aana chahiye.\n"
        'Reply ONLY with JSON: {"groups": [{"item_indices": '
        '[0, 1], "instruction": "...", "tools": '
        '["tool_name"]}]}')
    for _round in range(2):
        parsed = _ask(brain_factory, prompt)
        groups = parsed.get("groups") \
            if isinstance(parsed, dict) else None
        problems = validate_groups(groups or [], items,
                                   set(tool_names))
        if groups and not problems:
            return groups
        prompt += (
            "\nPichhle plan me ye khaamiyan theen, sudhaar "
            "kar do: " + "; ".join(problems or
                                   ["JSON samajh nahi aaya"]))
    return None


def jobs_for_groups(groups, items) -> list:
    """(job_text, group_tools) per (group, item) pair,
    sorted back into ITEM order — results come home in
    the order the owner listed the work."""
    jobs = []
    for group in groups:
        instruction = str(group.get("instruction", ""))
        tools = [str(t) for t in group.get("tools") or []]
        for ix in group.get("item_indices") or []:
            item = str(items[ix])
            text = instruction.replace("{item}", item)
            if "{item}" not in instruction:
                text = instruction + f"\n\nITEM: {item}"
            jobs.append((ix, text, tools))
    jobs.sort(key=lambda row: row[0])
    return [(text, tools) for _ix, text, tools in jobs]
