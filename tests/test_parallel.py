"""Simultaneity, measured: one round of independent calls runs
CONCURRENTLY (a counter proves the overlap), while a single
destructive call keeps the whole round serial — and results
always come home in call order."""

import threading
import time

from jarvis.brain import Brain


class ScriptedChain:
    name = "scripted"

    def __init__(self, calls):
        self._calls = calls
        self.round = 0

    def complete(self, messages, tools=None):
        self.round += 1
        if self.round == 1:
            return {"content": "", "tool_calls": self._calls}
        return {"content": "sab ho gaya, sir"}


class Meter:
    def __init__(self):
        self.lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.order = []

    def dispatch(self, name, args):
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            self.order.append(name)
        time.sleep(0.06)
        with self.lock:
            self.live -= 1
        return {"ok": True, "tool": name}


def _schema(name, destructive=False):
    out = {"name": name, "description": "",
           "parameters": {"type": "object", "properties": {}}}
    if destructive:
        out["destructive"] = True
    return out


def _run(calls, schemas):
    meter = Meter()
    brain = Brain(ScriptedChain(calls), schemas, meter.dispatch)
    result = brain.respond([], "ek saath teeno kaam karo")
    return meter, result


def test_independent_calls_run_at_once():
    calls = [{"name": n, "args": {}}
             for n in ("alpha", "beta", "gamma")]
    schemas = [_schema(n) for n in ("alpha", "beta", "gamma")]
    meter, result = _run(calls, schemas)
    assert meter.peak >= 2                     # the overlap is real
    assert [r.tool for r in result.tool_calls_made] == \
        ["alpha", "beta", "gamma"]             # order still honest
    assert "sab ho gaya" in result.reply


def test_one_destructive_call_keeps_the_round_serial():
    calls = [{"name": n, "args": {}}
             for n in ("alpha", "boom", "gamma")]
    schemas = [_schema("alpha"), _schema("boom", destructive=True),
               _schema("gamma")]
    meter, result = _run(calls, schemas)
    assert meter.peak == 1
    assert len(result.tool_calls_made) == 3
