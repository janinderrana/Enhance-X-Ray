"""Fake OpenAI-compatible servers reproducing real failure modes of the local provider.
    python3 test_local_provider.py [path/to/enrich_dir]"""
import json, re, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent))
import enrich
enrich.BACKOFF = (0.05, 0.05, 0.05)             # keep the test fast

def make_server(behaviour, life=None, reply=None, canon_of=None, entries_fn=None):
    """life: optional oMLX-like model state {'known','resident','pinned','events','status','load_code','unload_code'}"""
    log = {"requests": 0, "had_tool_choice": [], "had_schema": [], "rejections": 0}
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if life is None or not life.get("status", True) or not self.path.endswith("/models/status"):
                self.send_response(404); self.end_headers(); return
            models = [{"id": m, "loaded": m in life["resident"], "pinned": m in life["pinned"]} for m in life["known"]]
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps({"models": models}).encode())
        def do_POST(self):
            mm = re.match(r"^/v1/models/(.+)/(load|unload)$", self.path)
            if mm and life is not None:
                n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
                mid, op = urllib.parse.unquote(mm.group(1)), mm.group(2)
                life["events"].append((op, mid))
                code = life.get(op + "_code", 200)
                if code == 200:
                    (life["resident"].add if op == "load" else life["resident"].discard)(mid)
                self.send_response(code); self.end_headers(); self.wfile.write(b"{}"); return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if life is not None:
                life["events"].append(("chat", body.get("model"))); life["resident"].add(body.get("model"))
            log["requests"] += 1; log["had_tool_choice"].append("tool_choice" in body); log["had_schema"].append("response_format" in body)
            log.setdefault("calls", []).append((body.get("model"), body["messages"][1]["content"]))
            err = behaviour(body, log)
            if err:
                log["rejections"] += 1
                self.send_response(err[0]); self.end_headers(); self.wfile.write(json.dumps({"error": {"message": err[1]}}).encode()); return
            log.setdefault("bodies", []).append(body)
            names = re.findall(r"^### (.+?)  \(", body["messages"][1]["content"], re.M)
            prompt = body["messages"][1]["content"]
            if entries_fn:
                out = {"entries": entries_fn(names, prompt)}
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps(out)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode())
                return
            out = {"entries": [{"original_name": n, "canonical_name": canon_of(n, prompt) if canon_of else n,
                                "description": reply(n, prompt) if reply else f"desc of {n}"} for n in names]}
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps(out)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode())
    srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, log

work = [{"id": i, "name": f"Ent{i}", "type": 1, "count": 3, "excerpts": ["x" * 120]} for i in range(5)]
LOGS = []
def run(behaviour, work_=None, life=None, reply=None, canon_of=None, book=None, entries_fn=None, **opts_extra):
    srv, log = make_server(behaviour, life, reply, canon_of, entries_fn)
    kw = dict(base_url=f"http://127.0.0.1:{srv.server_port}/v1", model_bulk="m", model_high="", parallel=1, provider="local", people_layout="no")
    kw.update(opts_extra)
    LOGS.clear()
    try: res = enrich.enrich_local(work_ if work_ is not None else work, lambda m="": LOGS.append(m), SimpleNamespace(**kw), book=book)
    except enrich.Skip as e: res = f"SKIP: {e}"
    srv.shutdown(); return res, log

def check(label, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else "")); check.failed += (not ok)
check.failed = 0

# 1 healthy server: tool_choice sent once, schema used
res, log = run(lambda b, l: None)
check("healthy server: all 5 described, schema + tool_choice sent", len(res) == 5 and all(log["had_schema"]) and all(log["had_tool_choice"]))
# 2 strict OpenAI-style server: tool_choice without tools -> 400 naming tool_choice
res, log = run(lambda b, l: (400, "Invalid value for 'tool_choice': 'tool_choice' is only allowed when 'tools' are specified.") if "tool_choice" in b else None)
check("strict server (tool_choice 400): drops tool_choice, keeps schema, succeeds", len(res) == 5 and log["had_tool_choice"][-1] is False and log["had_schema"][-1] is True, f"requests={log['requests']}")
# 3 server without schema support
res, log = run(lambda b, l: (400, "response_format json_schema is not supported") if "response_format" in b else None)
check("no-schema server (400 names schema): falls back to prompt mode, succeeds, WARNS loudly", len(res) == 5 and log["had_schema"][-1] is False and log["had_tool_choice"][-1] is True and any("WARNING" in m and "JSON schema" in m for m in LOGS))
# 4 transient oMLX memory-guard rejection must NOT disable the schema
def mem(b, l): return (400, "oMLX prefill memory guard rejected this prompt: Prefill would require ~42 GB") if l["rejections"] < 2 else None
res, log = run(mem)
check("transient memory-guard 400s: waits, retries, schema stays ON, succeeds", len(res) == 5 and all(log["had_schema"]) and log["rejections"] == 2, f"requests={log['requests']}")
# 5 unexplained 400 whenever a schema is present
res, log = run(lambda b, l: (400, "Bad Request") if "response_format" in b else None)
check("unexplained 400 with schema: schema is NEVER dropped; clean Skip instead", isinstance(res, str) and res.startswith("SKIP") and all(log["had_schema"]) and not any("WARNING" in m for m in LOGS), f"requests={log['requests']}")
# 6 auth failure aborts cleanly
res, log = run(lambda b, l: (401, "bad key"))
check("401: clean Skip, no retries storm", isinstance(res, str) and res.startswith("SKIP") and log["requests"] == 1, str(res)[:60])
# 7 model missing
res, log = run(lambda b, l: (404, "model not found"))
check("404: clean Skip", isinstance(res, str) and "not found" in res)

# ---- v1.2.5: tiers, excerpts and batch packing -------------------------------------------------------------
import re as _re

def mk(n, count, k_exc, ln=400, start=0):
    return [{"id": start + i, "name": f"E{start + i}", "type": 1, "count": count(i) if callable(count) else count,
             "excerpts": ["y" * ln for _ in range(k_exc)]} for i in range(n)]

top, rest = enrich.split_top_tier(mk(150, lambda i: 300 - i, 8))
check("split_top_tier: 150 entities -> exactly the 100 most mentioned", len(top) == 100 and len(rest) == 50 and min(w["count"] for w in top) > max(w["count"] for w in rest))
top, rest = enrich.split_top_tier(mk(30, lambda i: 32 - i, 8))          # counts 32..3
check("split_top_tier: minimum 5 mentions (counts 32..3 -> 28 in top)", len(top) == 28 and all(w["count"] >= 5 for w in top) and all(w["count"] < 5 for w in rest))
top, rest = enrich.split_top_tier(mk(12, 2, 3))
check("split_top_tier: nobody reaches 5 mentions -> empty top tier", top == [] and len(rest) == 12)
check("split_top_tier: unsorted input is handled", [w["count"] for w in enrich.split_top_tier(mk(8, lambda i: [9, 50, 7, 30, 6, 5, 80, 12][i], 4))[0]][:3] == [80, 50, 30])

batches = enrich.pack_batches(mk(60, 10, 8, ln=600))                      # ~5k chars per entity
check("pack_batches: respects the character budget and the 20-entity cap",
      all(len(b) <= enrich.BATCH_SIZE for b in batches) and all(sum(enrich._entity_chars(w) for w in b) <= enrich.MAX_BATCH_CHARS + 6000 for b in batches)
      and sum(len(b) for b in batches) == 60 and len(batches) > 3, f"{len(batches)} batches")
check("pack_batches: small entities still obey the 20-entity cap", [len(b) for b in enrich.pack_batches(mk(45, 10, 1, ln=100))] == [20, 20, 5])
check("pack_batches: an oversized entity gets its own batch, nothing is lost", [len(b) for b in enrich.pack_batches(mk(3, 10, 8, ln=9000))] == [1, 1, 1])

# end to end against the fake server: top tier -> 'hi' with 8 excerpts, the rest -> 'lo' with 5
w2 = mk(130, lambda i: 400 - i if i < 110 else 3, 8, ln=300)
res, log = run(lambda b, l: None, w2, model_bulk="lo", model_high="hi", parallel=2)
calls = log["calls"]
def per_entity_excerpts(prompt):
    return [sec.count("\n- ") for sec in prompt.split("### ")[1:]]
def excerpt_totals(calls_):
    """entity name -> excerpts it was sent in total (v1.2.7 spreads them over several staged requests)"""
    tot = {}
    for _, p in calls_:
        for sec in p.split("### ")[1:]:
            tot[sec.split("  (")[0]] = tot.get(sec.split("  (")[0], 0) + sec.count("\n- ")
    return tot
hi_calls = [c for c in calls if c[0] == "hi"]; lo_calls = [c for c in calls if c[0] == "lo"]
hi_names = {n for _, p in hi_calls for n in _re.findall(r"^### (.+?)  \(", p, _re.M)}
check("local tiers: top tier = the 100 most-mentioned, sent to the top model", len(hi_names) == 100 and all(int(n[1:]) < 100 for n in hi_names), f"{len(hi_names)} names")
check("local tiers: top-tier entities get 8 excerpts in total", bool(hi_calls) and all(k == 8 for k in excerpt_totals(hi_calls).values()))
check("local tiers: bulk entities get 5 excerpts in total", bool(lo_calls) and all(k == 5 for k in excerpt_totals(lo_calls).values()))
check("local tiers: every request stays near the prompt budget", all(len(p) <= enrich.MAX_BATCH_CHARS + 8000 for _, p in calls), f"max {max(len(p) for _, p in calls):,} chars")
check("local tiers: all 130 entities described, tier line logged", len(res) == 130 and any("top-tier entities" in m for m in LOGS))
res, log = run(lambda b, l: None, w2, model_bulk="lo", model_high="", parallel=2)
check("local, no top-tier model: everything goes to the bulk model with 5 excerpts", len(res) == 130 and all(c[0] == "lo" for c in log["calls"]) and all(k == 5 for k in excerpt_totals(log["calls"]).values()))
check("local prompt: role/relationship guidance present; Claude prompt untouched", "relationships to other named people" in enrich.LOCAL_SYSTEM and "relationships to other named people" not in enrich.SYSTEM)


# ---- v1.2.6: bulk-model lifecycle (load before the bulk tier, unload after; top-tier model untouched) ------
def newlife(**kw):
    d = {"known": ["hi", "lo"], "resident": {"hi"}, "pinned": {"hi"}, "events": []}
    d.update(kw); return d
def idx(ev, e, last=False):
    hits = [i for i, x in enumerate(ev) if x == e]
    return (hits[-1] if last else hits[0]) if hits else None
w3 = mk(130, lambda i: 400 - i if i < 110 else 3, 8, ln=300)

L = newlife(); res, log = run(lambda b, l: None, w3, L, model_bulk="lo", model_high="hi", parallel=2)
ev = L["events"]
check("lifecycle: top chats -> load bulk -> bulk chats -> unload bulk, in that order",
      len(res) == 130 and idx(ev, ("chat", "hi"), True) < idx(ev, ("load", "lo")) < idx(ev, ("chat", "lo")) and ev[-1] == ("unload", "lo") and ev.count(("load", "lo")) == 1 and ev.count(("unload", "lo")) == 1)
check("lifecycle: the top-tier model is never loaded or unloaded by the plugin", ("unload", "hi") not in ev and ("load", "hi") not in ev and L["resident"] == {"hi"})
check("lifecycle: log says loaded and unloaded", any("loaded in" in m for m in LOGS) and any("unloaded; memory returned" in m for m in LOGS))

L = newlife(resident={"hi", "lo"}); res, log = run(lambda b, l: None, w3, L, model_bulk="lo", model_high="hi", parallel=2)
check("lifecycle: bulk model already resident -> not loaded, not unloaded", ("load", "lo") not in L["events"] and ("unload", "lo") not in L["events"] and "lo" in L["resident"] and any("already resident" in m for m in LOGS))

L = newlife(); res, log = run(lambda b, l: None, w3, L, model_bulk="lo", model_high="hi", parallel=2, unload_bulk="no")
check("lifecycle: unload-when-finished off -> loaded, left loaded", ("load", "lo") in L["events"] and ("unload", "lo") not in L["events"] and any("left loaded" in m for m in LOGS))

L = newlife(pinned={"hi", "lo"}); res, log = run(lambda b, l: None, w3, L, model_bulk="lo", model_high="hi", parallel=2)
check("lifecycle: pinned bulk model is loaded but never unloaded", ("load", "lo") in L["events"] and ("unload", "lo") not in L["events"] and any("pinned" in m for m in LOGS))

L = newlife(status=False); res, log = run(lambda b, l: None, w3, L, model_bulk="lo", model_high="hi", parallel=2)
check("lifecycle: server without /models/status -> no load/unload calls, run completes", len(res) == 130 and not [e for e in L["events"] if e[0] in ("load", "unload")])

L = newlife(load_code=507); res, log = run(lambda b, l: None, w3, L, model_bulk="lo", model_high="hi", parallel=2)
check("lifecycle: pre-load fails -> run continues (first batch loads it), still unloaded at the end", len(res) == 130 and L["events"][-1] == ("unload", "lo") and any("could not pre-load" in m for m in LOGS))

L = newlife(unload_code=202); res, log = run(lambda b, l: None, w3, L, model_bulk="lo", model_high="hi", parallel=2)
check("lifecycle: unload answers 202 (queued, as real oMLX does right after the last request) -> reported as queued, results intact", len(res) == 130 and any("unload queued" in m for m in LOGS) and not any("could not unload" in m for m in LOGS))
L = newlife(unload_code=409); res, log = run(lambda b, l: None, w3, L, model_bulk="lo", model_high="hi", parallel=2)
check("lifecycle: unload fails (409) -> non-fatal, reported, results intact", len(res) == 130 and any("could not unload" in m and "409" in m for m in LOGS))

L = newlife(resident=set(), pinned=set(), known=["lo"]); res, log = run(lambda b, l: None, w3, L, model_bulk="lo", model_high="", parallel=2)
check("lifecycle: no top-tier model -> single bulk model loaded, used, unloaded", len(res) == 130 and L["events"][0] == ("load", "lo") and L["events"][-1] == ("unload", "lo"))

L = newlife(); res, log = run(lambda b, l: (401, "bad key") if b["model"] == "lo" else None, w3, L, model_bulk="lo", model_high="hi", parallel=2)
check("lifecycle: failure during the bulk tier still unloads the bulk model", isinstance(res, str) and res.startswith("SKIP") and L["events"][-1] == ("unload", "lo"))

L = newlife(); res, log = run(lambda b, l: None, mk(50, 100, 8, ln=300), L, model_bulk="lo", model_high="hi", parallel=2)
check("lifecycle: nothing for the bulk tier -> bulk model never loaded or unloaded", len(res) == 50 and not [e for e in L["events"] if e[1] == "lo"])


# ---- same model selected for BOTH tiers -------------------------------------------------------------------
L = newlife(known=["q"], resident={"q"}, pinned={"q"})
res, log = run(lambda b, l: None, w3, L, model_bulk="q", model_high="q", parallel=2)
check("same model for both tiers, resident + pinned: never loaded, never unloaded", len(res) == 130 and not [e for e in L["events"] if e[0] in ("load", "unload")] and L["resident"] == {"q"})
res1, log1 = run(lambda b, l: None, w3, L, model_bulk="q", model_high="q", parallel=2, same_model_tiers="no")
check("same model + same_model_tiers=no: single-model mode (no top tier, 5 excerpts each)", any("130 entities -> q" in m for m in LOGS) and not any("top-tier" in m for m in LOGS) and all(k == 5 for k in excerpt_totals(log1["calls"]).values()))
res1, log1 = run(lambda b, l: None, w3, newlife(known=["q"], resident={"q"}, pinned={"q"}), model_bulk="q", model_high="q", parallel=2)
check("same model, default: the top entities still get the top-tier treatment (8 excerpts)", any("top-tier entities" in m for m in LOGS) and max(excerpt_totals(log1["calls"]).values()) == 8)
L = newlife(known=["q"], resident={"q"}, pinned=set())
res, log = run(lambda b, l: None, w3, L, model_bulk="q", model_high="q", parallel=2)
check("same model for both tiers, resident but NOT pinned: left loaded (it was already resident)", len(res) == 130 and ("unload", "q") not in L["events"])
L = newlife(known=["q"], resident=set(), pinned=set())
res, log = run(lambda b, l: None, w3, L, model_bulk="q", model_high="q", parallel=2)
check("same model for both tiers, not resident and not pinned: loaded for the run, unloaded at the end", len(res) == 130 and L["events"][0] == ("load", "q") and L["events"][-1] == ("unload", "q"))
L = newlife(known=["q"], resident=set(), pinned={"q"})
res, log = run(lambda b, l: None, w3, L, model_bulk="q", model_high="q", parallel=2)
check("same model for both tiers, not resident but pinned: loaded, never unloaded", len(res) == 130 and ("load", "q") in L["events"] and ("unload", "q") not in L["events"])
L = newlife(known=["hi", "lo"], resident={"hi"}, pinned={"hi"}); L["known"] = ["hi", "lo"]
res, log = run(lambda b, l: None, w3, L, model_bulk="lo", model_high="hi", parallel=2, unload_bulk="yes")
check("two DIFFERENT models (e.g. Qwen3.6 top + Qwen3.8 bulk): bulk loaded then unloaded, top untouched", L["events"].count(("unload", "lo")) == 1 and not [e for e in L["events"] if e[1] == "hi" and e[0] in ("load", "unload")])


# ---- layered cards (v1.2.7) --------------------------------------------------------------------------------
import tempfile, sqlite3
def ent(i, n, count=9, name=None):
    return {"id": i, "name": name or f"E{i}", "type": 1, "count": count,
            "excerpts": [f"ex{k} Karamchand Dewan Porbandar Rajkot Thakur Sahib Aleppo Gabriel Juliette " + "x" * 120 for k in range(n)],
            "spos": [k * 100 for k in range(n)]}
def stage_calls(log, name):
    out = []
    for _, pr in log["calls"]:
        for sec in pr.split("### "):
            if sec.startswith(name + "  ("):
                out.append(sec)
    return out
def ex_ids(sec): return [int(m) for m in _re.findall(r"^- ex(\d+) ", sec, _re.M)]
CARD1 = "Son of Karamchand, the former Dewan of Porbandar. Born in Rajkot."
def sec_of(n, prompt): return [x for x in prompt.split("### ")[1:] if x.startswith(n + "  (")][0]
def seq_reply(n, prompt):
    sec = sec_of(n, prompt)
    return ("Counsellor in Rajkot. " + _re.search(r"Current card: (.*)", sec).group(1)) if "Current card:" in sec else CARD1

cp = Path(tempfile.mkdtemp()) / "layers.json"
w = [ent(0, 8), ent(1, 5), ent(2, 4), ent(3, 6), ent(4, 3), ent(5, 1)]
res, log = run(lambda b, l: None, w, reply=seq_reply, model_bulk="m", model_high="hi", parallel=1, checkpoint=cp)
log_static = log
secs = {n: stage_calls(log, n) for n in ("E0", "E1", "E2", "E3", "E4", "E5")}
check("layers: stages per entity 8->3, 5->2, 4->2, 6->3, 3->1, 1->1", [len(secs[n]) for n in ("E0", "E1", "E2", "E3", "E4", "E5")] == [3, 2, 2, 3, 1, 1], str({n: len(v) for n, v in secs.items()}))
check("layers: excerpts are chronological, disjoint and complete (8 -> 3,3,2)", [ex_ids(x) for x in secs["E0"]] == [[0, 1, 2], [3, 4, 5], [6, 7]])
check("layers: stage 1 has no card, later stages carry the card so far", "Current card:" not in secs["E0"][0] and f"Current card: {CARD1}\n" in secs["E0"][1] and "Current card: Counsellor in Rajkot. " + CARD1 in secs["E0"][2])
check("layers: the update instruction is only in requests that carry a card, and leaves the order to the model", all(("CURRENT CARD" in pr) == ("Current card:" in pr) for _, pr in log["calls"]) and any("up to you" in pr for _, pr in log["calls"]))
check("layers: final card is the model's last rewrite; short entities are single-pass", res["E0"].description == "Counsellor in Rajkot. Counsellor in Rajkot. " + CARD1 and res["E4"].description == CARD1 and res["E5"].description == CARD1)
check("layers: all six entities described; a single-excerpt entity cost one request", len(res) == 6 and len(secs["E5"]) == 1)
check("layers: result.stages = card after each stage with the position of that stage's newest excerpt", [pos for _, pos in res["E0"].stages] == [None, 500, 700] and res["E0"].stages[0][0] == CARD1 and res["E0"].stages[2][0] == res["E0"].description, str([pos for _, pos in res["E0"].stages]))
check("layers: an entity with one stage has one page", len(res["E4"].stages) == 1 and res["E4"].stages[0][1] is None)
import json as _json
ck = _json.loads(cp.read_text(encoding="utf-8"))
check("layers: checkpoint JSON has the per-stage trail", [t["stage"] for t in ck["E0"]["trail"]] == [1, 2, 3] and ck["E0"]["card"] == res["E0"].description)
check("layers: summary line logged", any(m.strip().startswith("cards:") for m in LOGS))

# the model has the final say on what to keep; a card that drops names is reported, not rejected
def lossy(n, prompt): return CARD1 if "Current card:" not in sec_of(n, prompt) else "A boy in Rajkot who plays."
res, _ = run(lambda b, l: None, [ent(0, 6)], reply=lossy, model_bulk="m", model_high="hi", parallel=1)
check("layers: card that drops names is accepted and reported", res["E0"].description == "A boy in Rajkot who plays." and any("no longer mention some names" in m and "2 later cards" in m for m in LOGS), " | ".join(m for m in LOGS if "cards:" in m))
# unchanged later cards collapse into one page
res, _ = run(lambda b, l: None, [ent(0, 8)], reply=lambda n, pr: CARD1, model_bulk="m", model_high="hi", parallel=1)
check("layers: identical cards at later stages collapse into one page", len(res["E0"].stages) == 1)
# word ceiling: an over-long card is shortened by the model; if that fails, whole trailing sentences go
S2 = "Son of Karamchand, the former Dewan of Porbandar."
SHORT = "Son of Karamchand, the Dewan of Porbandar. Counsellor to the Thakur Sahib."
LONGC = S2 + " " + " ".join(["filler"] * 100) + "."
def over_reply(condensed):
    def f(n, pr):
        sec = sec_of(n, pr)
        if "Card to shorten" in sec: return condensed
        return LONGC if "Current card:" in sec else CARD1
    return f
res, log = run(lambda b, l: None, [ent(0, 4)], reply=over_reply(SHORT), model_bulk="m", model_high="hi", parallel=1)
cs = [pr for _, pr in log["calls"] if "Card to shorten" in pr]
check("condense: an over-long card triggers a request that shows the card and the limit; the model's shorter card is kept", res["E0"].description == SHORT and len(cs) == 1 and "the limit is 90" in cs[0] and "Current card:" not in cs[0] and "at most 90 words" in cs[0], res["E0"].description[:60])
check("condense: counted in the log (1 shortened by the model)", any("1 of them shortened by the model" in m for m in LOGS), " | ".join(m for m in LOGS if "cards:" in m)[:160])
check("condense: the stage keeps its position and the shorter card", res["E0"].stages[-1][0] == SHORT and res["E0"].stages[-1][1] == 300, str(res["E0"].stages[-1]))
res, _ = run(lambda b, l: None, [ent(0, 4)], reply=over_reply(LONGC), model_bulk="m", model_high="hi", parallel=1)
check("condense: if the model is still over the limit, whole trailing sentences are dropped", res["E0"].description == S2, res["E0"].description[:60])
res, _ = run(lambda b, l: None, [ent(0, 4)], reply=over_reply(""), model_bulk="m", model_high="hi", parallel=1)
check("condense: an empty reply falls back to dropping trailing sentences", res["E0"].description == S2)
ONE_LONG = " ".join(["Karamchand"] + ["word"] * 120) + "."
def one_long(condensed):
    def f(n, pr):
        sec = sec_of(n, pr)
        if "Card to shorten" in sec: return condensed
        return ONE_LONG if "Current card:" in sec else CARD1
    return f
res, _ = run(lambda b, l: None, [ent(0, 4)], reply=one_long(ONE_LONG), model_bulk="m", model_high="hi", parallel=1)
check("condense: one over-long sentence the model will not shorten -> the earlier card stays", res["E0"].description == CARD1 and any("kept an earlier card" in m and "cards: 1" in m for m in LOGS), " | ".join(m for m in LOGS if "cards:" in m)[:200])
check("condense: the layering note states the word limit", "must stay within 90 words" in enrich.LAYER_NOTE and "must stay within 90 words" in enrich.LAYER_NOTE_PA)
res, _ = run(lambda b, l: None, [ent(0, 4)], reply=lambda n, pr: CARD1 if "Current card:" not in sec_of(n, pr) else "", model_bulk="m", model_high="hi", parallel=1)
check("layers: an empty later reply keeps the earlier card", res["E0"].description == CARD1)
def fail_stage2(body, l): return (500, "boom") if "CURRENT CARD" in body["messages"][1]["content"] else None
res, _ = run(fail_stage2, [ent(0, 4)], reply=lambda n, pr: CARD1, model_bulk="m", model_high="hi", parallel=1)
check("layers: stage 2 request fails -> stage-1 card still written", res["E0"].description == CARD1 and any("no valid reply" in m for m in LOGS))
res, _ = run(lambda b, l: None, [ent(0, 4)], reply=lambda n, pr: "" if "ex0 " in pr else "Merchant in Aleppo.", model_bulk="m", model_high="hi", parallel=1)
check("layers: empty at stage 1 -> stage 2 writes the card from scratch", res["E0"].description == "Merchant in Aleppo." and res["E0"].stages[0][1] is None)
check("limit: the prompt asks for 30-60 words and allows up to 90; ceiling is 90", enrich.DESC_WORDS == 90 and "usually 30-60 words" in enrich.LOCAL_SYSTEM and "up to 90" in enrich.LOCAL_SYSTEM)

# ---- page-aware ---------------------------------------------------------------------------------------------
res, log = run(lambda b, l: None, [ent(0, 8)], reply=seq_reply, model_bulk="m", model_high="hi", parallel=1, page_aware="yes")
check("page-aware: system prompt allows what the excerpts show and forbids looking beyond", all("nothing" not in "" for _ in [0]) and enrich.LOCAL_SYSTEM_PA != enrich.LOCAL_SYSTEM and "never hint" in enrich.LOCAL_SYSTEM_PA and "Never mention plot outcomes" not in enrich.LOCAL_SYSTEM_PA and "Never state outcomes" not in enrich.LOCAL_SYSTEM_PA)
check("page-aware: requests use the page-aware prompts", all("Never state outcomes" not in pr for _, pr in log["calls"]) and any("beyond the newest excerpt" in pr for _, pr in log["calls"]))

check("page-aware stages: first card = earliest excerpt alone; 4 -> 1,2,1; 5 -> 1,2,2; 6 -> 1,2,2,1; 8 -> 1,3,2,2; 3 stays one pass", [[len(c) for c in enrich.split_stages(list(range(n)), True)] for n in (3, 4, 5, 6, 8)] == [[3], [1, 2, 1], [1, 2, 2], [1, 2, 2, 1], [1, 3, 2, 2]])
check("static stages unchanged by the page-aware option", [[len(c) for c in enrich.split_stages(list(range(n)))] for n in (4, 5, 6, 8)] == [[2, 2], [3, 2], [2, 2, 2], [3, 3, 2]])
res, log = run(lambda b, l: None, [ent(0, 8)], reply=seq_reply, model_bulk="m", model_high="hi", parallel=1, page_aware="yes")
sc = stage_calls(log, "E0")
check("page-aware run (a top-30 character, 8 excerpts available): first request carries only the earliest excerpt, then 2, 2, 2, 1", [len(ex_ids(x)) for x in sc] == [1, 2, 2, 2, 1] and ex_ids(sc[0]) == [0], str([ex_ids(x) for x in sc]))
check("page-aware run: the stage positions are the newest excerpt of each chunk (first page from the start, then 200, 400, 600, 700)", [pos for _, pos in res["E0"].stages] == [None, 200, 400, 600, 700], str([pos for _, pos in res["E0"].stages]))
check("local prompts forbid comments on the text", "Never comment on the excerpts" in enrich.LOCAL_SYSTEM and "Never comment on the excerpts" in enrich.LOCAL_SYSTEM_PA and "Never comment on the excerpts" not in enrich.SYSTEM)
META_REPLY = lambda n, pr: CARD1 if "Current card:" not in sec_of(n, pr) else "Sister of Aram (likely a typo in the source for sister-in-law)."
res, _ = run(lambda b, l: None, [ent(0, 6)], reply=META_REPLY, model_bulk="m", model_high="hi", parallel=1)
check("layers: a card that comments on the text is dropped, the earlier card stays", res["E0"].description == CARD1 and any("commented on the text" in m and "2 dropped" in m for m in LOGS), " | ".join(m for m in LOGS if "cards:" in m))
res, _ = run(lambda b, l: None, [ent(0, 1)], reply=lambda n, pr: "Probably a merchant; unclear from the excerpts.", model_bulk="m", model_high="hi", parallel=1)
check("layers: a first card that comments on the text leaves the entity as it was", len(res) == 0 or "E0" not in res)

# ---- guardrails: rules in every request + a spoiler guard in code ------------------------------------------------
check("rules: every static request carries the rules block above the entities", all("Rules for every entry below" in pr and "No spoilers" in pr and pr.index("Rules for every entry") < pr.index("###") for _, pr in log_static["calls"]))
res_pa, log_pa = run(lambda b, l: None, [ent(0, 8)], reply=seq_reply, model_bulk="m", model_high="hi", parallel=1, page_aware="yes")
check("rules: every page-aware request carries the page-aware rules (reader has read only up to the newest excerpt)", all("Rules for every entry below" in pr and "read only up to the newest excerpt" in pr for _, pr in log_pa["calls"]))
check("rules: hindsight ban is in the system prompt of both modes", "hindsight" in enrich.LOCAL_SYSTEM and "hindsight" in enrich.LOCAL_SYSTEM_PA)
c1, d1 = enrich.scrub("Son of Karamchand, the Dewan of Porbandar. He was killed in the end. He lives in Rajkot.")
check("scrub (static): drops outcome and hindsight sentences, keeps the rest", c1 == "Son of Karamchand, the Dewan of Porbandar. He lives in Rajkot." and d1 == 1, c1)
check("scrub (static): a card that is all outcome is rejected", enrich.scrub("He died young. Later he was arrested.") == (None, 2))
check("scrub: hindsight words go in either mode", enrich.scrub("Leader of the village. He eventually leads the defense.", seen="eventually")[0] == "Leader of the village." and enrich.scrub("Doomed from the start.", seen="")[0] is None)
check("scrub (page-aware): an outcome the excerpts show is kept", enrich.scrub("Friend of Vartkes, who has been arrested.", seen="... Vartkes was arrested yesterday ...")[0] == "Friend of Vartkes, who has been arrested.")
check("scrub (page-aware): an outcome the excerpts do not show is dropped (it came from the model's knowledge)", enrich.scrub("Gabriel's son. He is later found dead in his bed.", seen="Stephan helped capture the howitzers")[0] == "Gabriel's son.")
check("scrub: a card with no outcome or hindsight word is returned unchanged", enrich.scrub("A schoolmaster fond of poetry. Head of the school.", seen="") == ("A schoolmaster fond of poetry. Head of the school.", 0))
res, _ = run(lambda b, l: None, [ent(0, 2)], reply=lambda n, pr: "Son of Karamchand, the Dewan of Porbandar. He was killed by soldiers.", model_bulk="m", model_high="hi", parallel=1)
check("guard in the run (static): outcome sentence removed from the stored card, counted in the log", res["E0"].description == "Son of Karamchand, the Dewan of Porbandar." and any("1 sentences removed by the spoiler guard" in m for m in LOGS), " | ".join(m for m in LOGS if "cards:" in m))
res, _ = run(lambda b, l: None, [ent(0, 2)], reply=lambda n, pr: "He was killed by soldiers.", model_bulk="m", model_high="hi", parallel=1)
check("guard in the run (static): a card with nothing left is not written", "E0" not in res and any("nothing left after the spoiler guard" in m and "1 dropped" in m for m in LOGS))
def pa_death(n, pr):
    return "Gabriel's son. He is later found dead in his bed."
res, _ = run(lambda b, l: None, [ent(0, 6)], reply=pa_death, model_bulk="m", model_high="hi", parallel=1, page_aware="yes")
check("guard in the run (page-aware): a death the excerpts never mention is removed", res["E0"].description == "Gabriel's son.", res["E0"].description)
e_dead = ent(0, 6); e_dead["excerpts"] = [x + " The boy lay dead in his bed." if i == 0 else x for i, x in enumerate(e_dead["excerpts"])]
res, _ = run(lambda b, l: None, [e_dead], reply=lambda n, pr: "Gabriel's son. He lay dead in his bed.", model_bulk="m", model_high="hi", parallel=1, page_aware="yes")
check("guard in the run (page-aware): the same death is kept once an excerpt shows it", res["E0"].description == "Gabriel's son. He lay dead in his bed.", res["E0"].description)

def make_xray(path, entities, occs, erl=100000):
    """entities: [(id, label, type)], occs: {id: [starts]}"""
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE book_metadata (srl INTEGER, erl INTEGER, has_images TINYINT, has_excerpts TINYINT, show_spoilers_default TINYINT, num_people INTEGER, num_terms INTEGER, num_images INTEGER, preview_images TEXT);
    CREATE TABLE entity (id INTEGER PRIMARY KEY, label TEXT, loc_label INTEGER, type INTEGER, count INTEGER, has_info_card TINYINT);
    CREATE TABLE entity_description (text TEXT, source_wildcard TEXT, source INTEGER, entity INTEGER PRIMARY KEY);
    CREATE TABLE occurrence (entity INTEGER, start INTEGER, length INTEGER);
    CREATE TABLE type (id INTEGER PRIMARY KEY, label INTEGER, singular_label INTEGER, icon INTEGER, top_mentioned_entities TEXT);
    """)
    c.execute("INSERT INTO book_metadata VALUES (0, ?, 0, 0, 0, 0, 0, 0, '')", (erl,))
    c.executemany("INSERT INTO type VALUES (?, 0, 0, 0, '')", [(1,), (2,)])
    for eid, label, typ in entities:
        c.execute("INSERT INTO entity VALUES (?, ?, 7, ?, ?, 1)", (eid, label, typ, len(occs[eid])))
        c.execute("INSERT INTO entity_description VALUES ('raw excerpt', 'wc', NULL, ?)", (eid,))
        c.executemany("INSERT INTO occurrence VALUES (?, ?, ?)", [(eid, st, len(label)) for st in occs[eid]])
    c.commit(); c.close()

book = "".join(f"w{k:05d} " for k in range(14000))            # 98,000 chars; every offset has context
bpos, btxt = [0], [book]
d = Path(tempfile.mkdtemp())
starts = [8000 + i * 1400 for i in range(60)]                    # 60 mentions, 8,000 .. 90,600
make_xray(d / "a.asc", [(1, "Zed Ent", 1), (2, "Other", 1)], {1: starts, 2: [20000]})
wk, ents_, occ_ = enrich.collect(d / "a.asc", bpos, btxt, page_aware=True)
w1 = [x for x in wk if x["id"] == 1][0]
check("page-aware collect: 48 candidates spread over the first 95% of the mentions, positions kept", enrich.PAGE_SPAN == 0.95 and len(w1["excerpts"]) == 48 == len(w1["spos"]) and w1["spos"][0] == starts[0] and w1["spos"][-1] == starts[int(60 * enrich.PAGE_SPAN) - 1] and w1["spos"] == sorted(w1["spos"]), f"{w1['spos'][0]}..{w1['spos'][-1]}")
wk0, _, _ = enrich.collect(d / "a.asc", bpos, btxt, page_aware=False)
w10 = [x for x in wk0 if x["id"] == 1][0]
check("static collect: still the earliest mentions only (first 8 of the first 40%), positions kept", w10["spos"] == starts[:8] and len(w10["excerpts"]) == 8)
t8 = enrich._take(w1, 8, True)
check("page-aware _take: 8 excerpts spread over the pool, first and last kept", len(t8["excerpts"]) == 8 and t8["spos"][0] == w1["spos"][0] and t8["spos"][-1] == w1["spos"][-1])
check("static _take: the first n", enrich._take(w10, 5, False)["spos"] == starts[:5])
dup = enrich.collect  # duplicate labels: only the most-mentioned entry is worked on
make_xray(d / "b.asc", [(1, "Twin", 1), (2, "Twin", 1)], {1: starts[:10], 2: starts[:3]})
wk2, _, _ = enrich.collect(d / "b.asc", bpos, btxt)
check("collect: two rows with one label -> only the more-mentioned one is worked on", [x["id"] for x in wk2] == [1] and enrich.LAST_COLLECT["duplicate_labels"] == 1)

# apply(): one row per stage card
occs = [1000 * k for k in range(1, 11)]                          # 1000 .. 10000
def build(name):
    make_xray(d / name, [(1, "Zed Ent", 1), (2, "Other", 1)], {1: occs, 2: [500]})
    wk_, ents_, occ_ = enrich.collect(d / name, bpos, btxt)
    return wk_, ents_, occ_
STAGES = [("Card one.", None), ("Card two.", 4500), ("Card three.", 7500)]
R = {"Zed Ent": SimpleNamespace(original_name="Zed Ent", canonical_name="Ent", description="Card three.", grounded=True, stages=STAGES)}
make_xray(d / "p.asc", [(1, "Zed Ent", 1), (2, "Other", 1)], {1: occs, 2: [500]})
wk_, ents_, occ_ = enrich.collect(d / "p.asc", bpos, btxt)
enrich.apply(d / "p.asc", wk_, ents_, occ_, R, lambda m="": LOGS.append(m), allow_merge=False, page_aware=True)
c = sqlite3.connect(d / "p.asc")
rows = c.execute("SELECT e.id, e.label, e.count, d.text FROM entity e JOIN entity_description d ON d.entity = e.id WHERE e.label = 'Ent' ORDER BY e.id").fetchall()
per = {r_[0]: c.execute("SELECT start, length FROM occurrence WHERE entity = ? ORDER BY start", (r_[0],)).fetchall() for r_ in rows}
check("page-aware apply: three rows with one label, one card each", [r_[3] for r_ in rows] == ["Card one.", "Card two.", "Card three."], str(rows))
check("page-aware apply: occurrences split at the stage positions (before 4500 / 4500-7499 / 7500+)", [[st - 4 for st, _ in per[r_[0]]] for r_ in rows] == [[1000, 2000, 3000, 4000], [5000, 6000, 7000], [8000, 9000, 10000]])
check("page-aware apply: renamed spans shifted on every row (Zed Ent -> Ent), counts match occurrences", all(ln == 3 for r_ in rows for _, ln in per[r_[0]]) and [r_[2] for r_ in rows] == [4, 3, 3])
check("page-aware apply: no occurrence lost or orphaned; metadata counts the rows", c.execute("SELECT count(*) FROM occurrence").fetchone()[0] == 11 and c.execute("SELECT count(*) FROM occurrence WHERE entity NOT IN (SELECT id FROM entity)").fetchone()[0] == 0 and c.execute("SELECT num_people FROM book_metadata").fetchone()[0] == 4)
check("page-aware apply: the other entity is untouched", c.execute("SELECT count(*) FROM entity WHERE label = 'Other'").fetchone()[0] == 1)
check("page-aware apply: log mentions the extra cards", any("page-aware: 1 characters now have 2 extra cards" in m for m in LOGS))
c.close()
make_xray(d / "q.asc", [(1, "Zed Ent", 1), (2, "Other", 1)], {1: occs, 2: [500]})
wk_, ents_, occ_ = enrich.collect(d / "q.asc", bpos, btxt)
enrich.apply(d / "q.asc", wk_, ents_, occ_, R, lambda m="": LOGS.append(m), allow_merge=False, page_aware=False)
c = sqlite3.connect(d / "q.asc")
check("static apply (page_aware off): one row with the final card, all occurrences", c.execute("SELECT count(*) FROM entity WHERE label = 'Ent'").fetchone()[0] == 1 and c.execute("SELECT d.text FROM entity e JOIN entity_description d ON d.entity = e.id WHERE e.label = 'Ent'").fetchone()[0] == "Card three." and c.execute("SELECT count(*) FROM occurrence WHERE entity = 1").fetchone()[0] == 10)
c.close()
R2 = {"Zed Ent": SimpleNamespace(original_name="Zed Ent", canonical_name="Ent", description="Card two.", grounded=True, stages=[("Card one.", None), ("Card two.", None)])}
make_xray(d / "r.asc", [(1, "Zed Ent", 1), (2, "Other", 1)], {1: occs, 2: [500]})
wk_, ents_, occ_ = enrich.collect(d / "r.asc", bpos, btxt)
enrich.apply(d / "r.asc", wk_, ents_, occ_, R2, lambda m="": LOGS.append(m), allow_merge=False, page_aware=True)
c = sqlite3.connect(d / "r.asc")
check("page-aware apply: stage without a position -> no split, final card", c.execute("SELECT count(*) FROM entity WHERE label = 'Ent'").fetchone()[0] == 1)
c.close()
R3 = {"Zed Ent": SimpleNamespace(original_name="Zed Ent", canonical_name="Ent", description="Card three.", grounded=True, stages=[("Card one.", None), ("Card two.", 10**9), ("Card three.", 10**9 + 5)])}
make_xray(d / "s.asc", [(1, "Zed Ent", 1), (2, "Other", 1)], {1: occs, 2: [500]})
wk_, ents_, occ_ = enrich.collect(d / "s.asc", bpos, btxt)
enrich.apply(d / "s.asc", wk_, ents_, occ_, R3, lambda m="": LOGS.append(m), allow_merge=False, page_aware=True)
c = sqlite3.connect(d / "s.asc")
check("page-aware apply: a stage that would hold no occurrence gets no row", c.execute("SELECT count(*) FROM entity WHERE label = 'Ent'").fetchone()[0] == 1 and c.execute("SELECT count(*) FROM entity_description WHERE entity NOT IN (SELECT id FROM entity)").fetchone()[0] == 0)
c.close()

check("fine pages: the main characters get up to five chunks after the first (12 -> 1,3,2,2,2,2; 8 -> 1,2,2,2,1), others stay at three", [[len(c) for c in enrich.split_stages(list(range(n)), True, True)] for n in (12, 8)] == [[1, 3, 2, 2, 2, 2], [1, 2, 2, 2, 1]] and [[len(c) for c in enrich.split_stages(list(range(n)), True, False)] for n in (12, 8)] == [[1, 4, 4, 3], [1, 3, 2, 2]])
wmany = [ent(i, 12, count=1000 - i) for i in range(33)]
res, log = run(lambda b, l: None, wmany, reply=seq_reply, model_bulk="m", model_high="hi", parallel=2, page_aware="yes")
check("fine pages in a run: the top 30 get 12 excerpts (6 requests), the 31st-33rd get 8 (4 requests)", len(stage_calls(log, "E0")) == 6 and len(stage_calls(log, "E29")) == 6 and len(stage_calls(log, "E30")) == 4 and sum(len(ex_ids(x)) for x in stage_calls(log, "E0")) == 12 and sum(len(ex_ids(x)) for x in stage_calls(log, "E31")) == 8, str([len(stage_calls(log, f"E{i}")) for i in (0, 29, 30, 32)]))
res, log = run(lambda b, l: None, wmany, reply=seq_reply, model_bulk="m", model_high="hi", parallel=2)
check("static cards: no fine pages (12 excerpts are not used, 8 at most)", max(sum(len(ex_ids(x)) for x in stage_calls(log, f"E{i}")) for i in (0, 29, 31)) == 8)

# ---- collision rescue ------------------------------------------------------------------------------------------
def rescue_book(path, src_surface="Zed", src_n=300, tgt_n=3, tgt_wiki=False):
    """entity 1 'Zed Sets Out' (the highlight is src_surface), entity 2 'Zed Bagradian'; the model will rename 1 -> 'Zed Bagradian'"""
    chars = ["."] * 60000
    starts1 = [1000 + i * 150 for i in range(src_n)]
    for st in starts1: chars[st:st + len(src_surface)] = list(src_surface)
    starts2 = [58000 + i * 150 for i in range(tgt_n)]
    for st in starts2: chars[st:st + 3] = list("Zed")
    text = "".join(chars)
    make_xray(path, [(1, "Zed Sets Out", 1), (2, "Zed Bagradian", 1)], {1: starts1, 2: starts2}, erl=60000)
    c = sqlite3.connect(path)
    c.execute("UPDATE entity SET count = (SELECT count(*) FROM occurrence WHERE occurrence.entity = entity.id)")
    c.execute("UPDATE occurrence SET length = ? WHERE entity = 1", (len(src_surface),))
    c.execute("UPDATE occurrence SET length = 3 WHERE entity = 2")
    if tgt_wiki: c.execute("UPDATE entity_description SET source = 1 WHERE entity = 2")
    c.commit(); c.close()
    return [0], [text]
def rescue_run(name, rescue=True, **kw):
    pos_, txt_ = rescue_book(d / name, **kw)
    wk_, ents_, occ_ = enrich.collect(d / name, pos_, txt_)
    RR = {w_["name"]: SimpleNamespace(original_name=w_["name"], canonical_name="Zed Bagradian", description="A boy of the Bagradian family.", grounded=True, stages=[("A boy of the Bagradian family.", None)]) for w_ in wk_}
    out = []
    enrich.apply(d / name, wk_, ents_, occ_, RR, lambda m="": out.append(m), allow_merge=False, rescue=rescue)
    c = sqlite3.connect(d / name)
    rows = c.execute("SELECT e.id, e.label, e.count, d.text FROM entity e JOIN entity_description d ON d.entity = e.id ORDER BY e.id").fetchall()
    n_occ = c.execute("SELECT count(*) FROM occurrence").fetchone()[0]; orph = c.execute("SELECT count(*) FROM occurrence WHERE entity NOT IN (SELECT id FROM entity)").fetchone()[0]
    c.close()
    return rows, n_occ, orph, out, ents_
rows, n_occ, orph, out, ents_ = rescue_run("rs1.asc")
check("collect: the highlighted text of each work entity is recorded (surface)", ents_[1]["surface"] == "zed")
check("rescue: 300-mention entity renamed onto a 3-mention entity's name is merged (one row, all occurrences kept)", len(rows) == 1 and rows[0][1] == "Zed Bagradian" and rows[0][2] == 303 and n_occ == 303 and orph == 0, str(rows))
check("rescue: the survivor keeps the model's card; the log says what was rescued", rows[0][3] == "A boy of the Bagradian family." and any(m.strip().startswith("rescue: 'Zed Sets Out' (300 mentions)") and "shown in the book as 'zed'" in m for m in out), " | ".join(out)[:200])
rows, n_occ, orph, out, _ = rescue_run("rs2.asc", src_n=10, tgt_n=3)
check("rescue: not when the entity has less than 5x the target's mentions (10 vs 3) -> left as WordDumb made it, reported", len(rows) == 2 and rows[0][1] == "Zed Sets Out" and any("needs review: 'Zed Sets Out' (10 mentions)" in m for m in out), str([r_[1] for r_ in rows]))
rows, n_occ, orph, out, _ = rescue_run("rs3.asc", tgt_wiki=True)
check("rescue: not when the target is a Wikipedia entry", len(rows) == 2 and rows[0][1] == "Zed Sets Out")
rows, n_occ, orph, out, _ = rescue_run("rs4.asc", src_surface="Qux")
check("rescue: not when the highlighted text is not inside the new name (highlight 'Qux')", len(rows) == 2 and rows[0][1] == "Zed Sets Out")
rows, n_occ, orph, out, _ = rescue_run("rs5.asc", rescue=False)
check("rescue off (--rescue no): the rename stays blocked, as in v1.2.7", len(rows) == 2 and rows[0][1] == "Zed Sets Out" and not any("rescue:" in m for m in out))
check("rescue constants: ratio 5", enrich.RESCUE_RATIO == 5)

# ---- names settled by vote; every collision source judged on its own (v1.2.9) ----------------------------------
sn = enrich.settle_name
check("settle_name: majority wins", sn(["A", "A", "B"], "O") == "A" and sn(["B"], "O") == "B" and sn(["A", "B", "C", "A"], "O") == "A")
check("settle_name: a tie or no clear lead keeps the entity's own label", sn(["A", "B"], "O") == "O" and sn(["A", "A", "B", "B", "C"], "O") == "O" and sn(["A", "B", "C", "D"], "O") == "O" and sn([], "O") == "O")
check("settle_name: the original label counts as a vote (Bagradian Effendi: Effendi x1, Gabriel x3, Stephan x2 -> Gabriel)", sn(["Bagradian Effendi", "Gabriel", "Gabriel", "Stephan", "Gabriel", "Stephan"], "Bagradian Effendi") == "Gabriel")
seqs = {}
def flip(order):
    def f(n, pr):
        seqs[n] = seqs.get(n, 0) + 1
        return order[(seqs[n] - 1) % len(order)]
    return f
seqs.clear()
res, _ = run(lambda b, l: None, [ent(0, 6, name="Bagradian")], reply=seq_reply, canon_of=flip(["Gabriel Bagradian", "Gabriel Bagradian", "Stephan Bagradian"]), model_bulk="m", model_high="hi", parallel=1)
check("names by vote in a run: Gabriel, Gabriel, Stephan -> Gabriel Bagradian (the last stage does not decide)", res["Bagradian"].canonical_name == "Gabriel Bagradian", res["Bagradian"].canonical_name)
seqs.clear()
res, _ = run(lambda b, l: None, [ent(0, 6, name="Bagradian")], reply=seq_reply, canon_of=flip(["Gabriel Bagradian", "Stephan Bagradian", "Haik Bagradian"]), model_bulk="m", model_high="hi", parallel=1)
check("names by vote in a run: three different names -> the entity keeps its own label, and the log says so", res["Bagradian"].canonical_name == "Bagradian" and any("given different names at different stages" in m for m in LOGS), res["Bagradian"].canonical_name)

def multi_book(path):
    """1 'Zed Sets Out' (300, highlight Zed), 2 'Zed Bagradian' (3), 3 'Bagradian Effendi' (65, highlight Effendi), 4 'Young Bagradian' (1);
    the model renames 1, 3 and 4 onto 'Zed Bagradian'"""
    chars = ["."] * 120000
    def put(starts, word):
        for st in starts: chars[st:st + len(word)] = list(word)
    s1 = [1000 + i * 150 for i in range(300)]; put(s1, "Zed")
    s2 = [58000 + i * 150 for i in range(3)]; put(s2, "Zed")
    s3 = [70000 + i * 150 for i in range(65)]; put(s3, "Effendi")
    s4 = [100000]; put(s4, "Young Bagradian")
    make_xray(path, [(1, "Zed Sets Out", 1), (2, "Zed Bagradian", 1), (3, "Bagradian Effendi", 1), (4, "Young Bagradian", 1)], {1: s1, 2: s2, 3: s3, 4: s4}, erl=120000)
    c = sqlite3.connect(path)
    c.execute("UPDATE entity SET count = (SELECT count(*) FROM occurrence WHERE occurrence.entity = entity.id)")
    for eid, ln in ((1, 3), (2, 3), (3, 7), (4, 15)): c.execute("UPDATE occurrence SET length = ? WHERE entity = ?", (ln, eid))
    c.commit(); c.close()
    return [0], ["".join(chars)]
pos_, txt_ = multi_book(d / "m1.asc")
wk_, ents_, occ_ = enrich.collect(d / "m1.asc", pos_, txt_)
RR = {w_["name"]: SimpleNamespace(original_name=w_["name"], canonical_name="Zed Bagradian", description=f"Card of {w_['name']}.", grounded=True, stages=[(f"Card of {w_['name']}.", None)]) for w_ in wk_}
out = []
enrich.apply(d / "m1.asc", wk_, ents_, occ_, RR, lambda m="": out.append(m), allow_merge=False)
c = sqlite3.connect(d / "m1.asc")
rows = {r_[0]: r_[1:] for r_ in c.execute("SELECT e.id, e.label, e.count, d.text FROM entity e JOIN entity_description d ON d.entity = e.id")}
check("rescue with several sources renamed onto one name: only the source that passes the tests is merged (Zed Sets Out absorbs Zed Bagradian)", 2 not in rows and rows[1][0] == "Zed Bagradian" and rows[1][1] == 303, str(rows))
check("rescue with several sources: the others keep their own label and card (Bagradian Effendi, Young Bagradian)", rows[3][0] == "Bagradian Effendi" and rows[3][1] == 65 and rows[4][0] == "Young Bagradian", str(rows))
check("rescue with several sources: no occurrence lost, none orphaned", c.execute("SELECT count(*) FROM occurrence").fetchone()[0] == 300 + 3 + 65 + 1 and c.execute("SELECT count(*) FROM occurrence WHERE entity NOT IN (SELECT id FROM entity)").fetchone()[0] == 0)
check("rescue with several sources: the log names the rescue and lists the others for review", any(m.strip().startswith("rescue: 'Zed Sets Out'") for m in out) and any("needs review: 'Bagradian Effendi'" in m and "belongs to the rescued entity" in m for m in out), " | ".join(out)[:300])
c.close()

# ---- v1.2.10: relatives must be in the evidence, names must be in the book, the book's own article, name hints ----
gr = enrich.guard_relations
c_, n_ = gr("The son of Gabriel and Hovsannah Tomasian. A young boy.", "Juliette: you are just like Stephan.", "Stephan Sets Out")
check("guard_relations: a family tie naming people the excerpts never mention is dropped", c_ == "A young boy." and n_ == 1, str((c_, n_)))
check("guard_relations: kept when the excerpts mention every name", gr("The son of Gabriel and Hovsannah Tomasian.", "Gabriel and Hovsannah Tomasian came in", "Stephan")[0] == "The son of Gabriel and Hovsannah Tomasian.")
check("guard_relations: the entity's own name, titles and possessives are fine", gr("Nephew of Pastor Aram, and Juliette’s son.", "Aram and Juliette were there", "Stephan Bagradian")[0] is not None and gr("Stephan Bagradian’s brother lives.", "x", "Stephan Bagradian")[0] is not None)
check("guard_relations: a title does not save an unmentioned name", gr("Nephew of Pastor Aram.", "nobody", "Stephan")[0] is None)
check("guard_relations: sentences without a family tie are not checked", gr("He walks with Shatakhian.", "nothing", "Stephan")[0] == "He walks with Shatakhian.")
bk = enrich.norm_text("Stephan Bagradian and Haik. Stephan Bagradian again. Stephan Bagradian")
check("name_attested: the book's own full name, a part of the label and no book (tests) pass; an invented surname does not", enrich.name_attested("Stephan Bagradian", "Stephan Sets Out", bk) and enrich.name_attested("Stephan", "Stephan Sets Out", bk) and enrich.name_attested("X Y", "Z", None) and not enrich.name_attested("Stephan Tomasian", "Stephan Sets Out", bk))
def hallucinate(n, pr):
    return "The son of Gabriel and Hovsannah Tomasian. He is a young boy." if "Current card:" not in sec_of(n, pr) else ""
e_h = ent(0, 2, name="Stephan")
e_h["excerpts"] = ["Juliette sat down beside him. “You’re just like Stephan.” " + "x" * 120, "ex1 " + "x" * 120]
res, _ = run(lambda b, l: None, [e_h], reply=hallucinate, model_bulk="m", model_high="hi", parallel=1)
check("guard in a run: invented parents are removed from the card and counted in the log", res["Stephan"].description == "He is a young boy." and any("1 sentences removed for naming a relative the excerpts never mention" in m for m in LOGS), " | ".join(m for m in LOGS if "cards:" in m)[:160])
res, _ = run(lambda b, l: None, [dict(e_h, excerpts=e_h["excerpts"][:1], spos=[0])], reply=lambda n, pr: "The son of Gabriel and Hovsannah Tomasian.", model_bulk="m", model_high="hi", parallel=1)
check("guard in a run: a card that is only an invented relationship is not written", "Stephan" not in res and any("1 cards dropped whole" in m for m in LOGS))
# names the book never uses
bookS = enrich.norm_text("Zed Bagradian was here. Zed Bagradian again. Zed Bagradian once more. Zed ran.")
e_n = dict(ent(0, 1, name="Zed Sets Out"), surface="zed")
res, _ = run(lambda b, l: None, [e_n], reply=lambda n, pr: "Gabriel's son Zed.", canon_of=lambda n, pr: "Zed Tomasian", book=bookS, model_bulk="m", model_high="hi", parallel=1)
check("names: a proposed name the book never uses is replaced; the label is a chapter-title-like non-name, so the highlighted word is used (Zed)", res["Zed Sets Out"].canonical_name == "Zed" and any("1 proposed names dropped because the book never uses them" in m for m in LOGS), res["Zed Sets Out"].canonical_name)
res, _ = run(lambda b, l: None, [e_n], reply=lambda n, pr: "Gabriel's son Zed.", canon_of=lambda n, pr: "Zed Bagradian", book=bookS, model_bulk="m", model_high="hi", parallel=1)
check("names: a proposed name the book does use is accepted", res["Zed Sets Out"].canonical_name == "Zed Bagradian")
bookL = enrich.norm_text("Iskuhi Tomasian. " * 6 + "Iskuhi walks.")
e_l = dict(ent(0, 1, name="Iskuhi Tomasian"), surface="iskuhi")
res, _ = run(lambda b, l: None, [e_l], reply=lambda n, pr: "Gabriel's friend.", canon_of=lambda n, pr: "Iskuhi Bagradian", book=bookL, model_bulk="m", model_high="hi", parallel=1)
check("names: an invented name for a label the book does use keeps the label", res["Iskuhi Tomasian"].canonical_name == "Iskuhi Tomasian")
check("layering note: later stages are told to correct a card the new excerpts contradict", "contradict the current card" in enrich.LAYER_NOTE and "contradict the current card" in enrich.LAYER_NOTE_PA)

def bogus_book(path):
    chars = ["."] * 40000
    starts = [2000 + i * 300 for i in range(20)]
    for st in starts: chars[st:st + 3] = list("Zed")
    make_xray(path, [(1, "Juliette Zed", 1), (2, "Gabriel Zed", 1), (3, "Musa Place", 2), (4, "The Forty Days", 1)], {1: starts, 2: starts[:10], 3: starts[:6], 4: starts[:5]}, erl=40000)
    c = sqlite3.connect(path)
    c.execute("UPDATE occurrence SET length = 3")
    for eid, text in ((1, "The Forty Days of Musa Dagh (German: Die vierzig Tage) is a 1933 novel"), (2, "Gabriel Zed is the protagonist of the 1933 novel"), (3, "The Forty Days of Musa Dagh is a novel"), (4, "The Forty Days of Musa Dagh is a 1933 novel")):
        c.execute("UPDATE entity_description SET source = 1, text = ? WHERE entity = ?", (text, eid))
    c.commit(); c.close()
    return [0], ["".join(chars)]
pos_, txt_ = bogus_book(d / "bk.asc")
wk_, ents_, occ_ = enrich.collect(d / "bk.asc", pos_, txt_, title="The Forty Days of Musa Dagh")
check("book's own article: a character whose Wikipedia text is the novel's article is treated as undescribed (and logged)", 1 in [w_["id"] for w_ in wk_] and enrich.LAST_COLLECT["book_article"] == ["Juliette Zed"] and ents_[1]["source"] is None, str([w_["id"] for w_ in wk_]))
check("fictional characters: a Wikipedia text that makes the entry the protagonist of a novel is not used (Gabriel Zed), and it is logged", 2 in [w_["id"] for w_ in wk_] and enrich.LAST_COLLECT["fictional"] == ["Gabriel Zed"] and ents_[2]["source"] is None, str(enrich.LAST_COLLECT["fictional"]))
check("book's own article: a term and an entry named like the book are left alone", 3 not in [w_["id"] for w_ in wk_] and 4 not in [w_["id"] for w_ in wk_])
wk_, _, _ = enrich.collect(d / "bk.asc", pos_, txt_)
check("without a title only the fictional-character rule applies (Gabriel Zed yes, the book's own article on Juliette Zed no)", [w_["id"] for w_ in wk_] == [2])
F = enrich.FICTION
check("fictional rule: matches 'is a fictional character', 'fictional town', 'protagonist of the 1933 novel', 'character in the 1851 novel'", all(F.search(t) for t in (
    "Frodo Baggins is a fictional character in J. R. R. Tolkien's writings", "Macondo (Spanish pronunciation: [maxkondo]) is a fictional town described in a novel",
    "Gabriel Bagradian is the protagonist of the 1933 novel The Forty Days of Musa Dagh", "Queequeg is a character in the 1851 novel Moby-Dick",
    "William of Baskerville is a fictional Franciscan friar from the 1980 historical mystery novel")))
check("fictional rule: leaves real people, biblical and mythical figures alone", not any(F.search(t[:300]) for t in (
    "Frederick Courteney Selous (1851 - 1917) was a British explorer. His adventures inspired Sir Henry Rider Haggard to create the fictional character Allan Quatermain.",
    "Ezra is the main character of the Book of Ezra. According to the Hebrew Bible,", "Gilgamesh was a hero in ancient Mesopotamian mythology and the protagonist of the Epic of Gilgamesh",
    "Enver Pasha (1881 - 1922) was an Ottoman military officer and a leader of the Young Turks.", "Damascus is the capital and largest city of Syria.")))
chars_ = ["."] * 20000
st_ = [2000 + i * 200 for i in range(8)]
for q_ in st_: chars_[q_:q_ + 3] = list("Zed")
make_xray(d / "late.asc", [(1, "Late Zed", 1), (2, "Early Zed", 1)], {1: st_, 2: st_}, erl=20000)
c_ = sqlite3.connect(d / "late.asc"); c_.execute("UPDATE occurrence SET length = 3")
c_.execute("UPDATE entity_description SET source = 1, text = ? WHERE entity = 1", ("A real person described at length. " * 9 + "He inspired a novel; the hero is a fictional character.",))
c_.execute("UPDATE entity_description SET source = 1, text = ? WHERE entity = 2", ("Early Zed is a fictional character in a novel.",))
c_.commit(); c_.close()
wk_, _, _ = enrich.collect(d / "late.asc", [0], ["".join(chars_)])
check("fictional rule in collect: only the first 300 characters of the Wikipedia text count (late mention ignored, early one used)", [w_["id"] for w_ in wk_] == [2], str([w_["id"] for w_ in wk_]))

def hint_book(path):
    chars = ["."] * 60000
    s1 = [1000 + i * 150 for i in range(300)]
    for st in s1: chars[st:st + 3] = list("Zed")
    s2 = [58000 + i * 400 for i in range(5)]
    for st in s2: chars[st:st + 13] = list("Zed Bagradian")
    make_xray(path, [(1, "Zed Sets Out", 1), (2, "Zed Bagradian", 1)], {1: s1, 2: s2}, erl=60000)
    c = sqlite3.connect(path)
    c.execute("UPDATE entity SET count = (SELECT count(*) FROM occurrence WHERE occurrence.entity = entity.id)")
    c.execute("UPDATE occurrence SET length = 3 WHERE entity = 1"); c.execute("UPDATE occurrence SET length = 13 WHERE entity = 2")
    c.commit(); c.close()
    return [0], ["".join(chars)]
pos_, txt_ = hint_book(d / "ht.asc")
wk_, ents_, occ_ = enrich.collect(d / "ht.asc", pos_, txt_)
w1_ = [w_ for w_ in wk_ if w_["id"] == 1][0]
check("name hints: an entity highlighted as 'zed' is told about the book's full name 'Zed Bagradian' (5 mentions)", w1_.get("hints") == [("Zed Bagradian", 5)], str(w1_.get("hints")))
res, log = run(lambda b, l: None, [w1_], reply=lambda n, pr: "A boy.", model_bulk="m", model_high="hi", parallel=1)
check("name hints: the request names them and warns against inventing a surname", any("Other entries in this book with this name: 'Zed Bagradian' (5 mentions)" in pr and "never invent a surname" in pr for _, pr in log["calls"]))

# ---- v1.2.11: people cards in the fixed layout: ties first, then role, then progress ----------------------------------
def pent(name, excerpts, count=100, typ=1):
    return {"id": hash(name) % 100000, "name": name, "type": typ, "count": count, "excerpts": list(excerpts), "spos": [k * 100 for k in range(len(excerpts))]}
def pe(name, role="", progress="", rels=()):
    return {"original_name": name, "canonical_name": name, "role": role, "progress": progress,
            "relations": [{"relation": r, "other": o, "quote": q} for r, o, q in rels]}
G_EX = ["Gabriel Bagradian was an Ottoman officer in the reserve. His wife Juliette smiled at Gabriel Bagradian.",
        "Gabriel gave his son Stephan an order: report twice a day."]
Q_WIFE = "His wife Juliette smiled at Gabriel Bagradian."
Q_SON = "Gabriel gave his son Stephan an order: report twice a day."
def world(per):
    """per: name -> pe(...) entry; every person not listed gets an empty entry; terms get a plain description"""
    def f(names, prompt):
        if "Write the cards of these people" in prompt:
            return [per.get(n, pe(n)) for n in names]
        return [{"original_name": n, "canonical_name": n, "description": "A term card."} for n in names]
    return f
trio = lambda: [pent("Gabriel Bagradian", G_EX, 1000), pent("Juliette Bagradian", ["Juliette smiled. " + "x" * 100], 600), pent("Stephan Bagradian", ["Stephan nodded. " + "x" * 100], 590)]
per = {"Gabriel Bagradian": pe("Gabriel Bagradian", "Armenian reserve officer", "Leads the defense.",
                               [("husband", "Juliette", Q_WIFE), ("father", "Stephan", Q_SON)])}
res, log = run(lambda b, l: None, trio(), entries_fn=world(per), people_layout="yes", model_bulk="m", model_high="hi", parallel=1)
check("people layout: the card is ties, then role, then progress", res["Gabriel Bagradian"].description == "Husband of Juliette Bagradian, father of Stephan Bagradian. Armenian reserve officer. Leads the defense.", res["Gabriel Bagradian"].description)
check("people layout: each tie also appears on the other person's card (husband -> wife; father -> child when the gender is unknown)", res["Juliette Bagradian"].description == "Wife of Gabriel Bagradian." and res["Stephan Bagradian"].description == "Child of Gabriel Bagradian.", f"{res.get('Juliette Bagradian') and res['Juliette Bagradian'].description} | {res.get('Stephan Bagradian') and res['Stephan Bagradian'].description}")
check("people layout: people get their own system prompt and schema (relations), terms keep the description schema", any("relations" in b["response_format"]["json_schema"]["schema"]["properties"]["entries"]["items"]["properties"] and "this person is the father of Stephan" in b["messages"][0]["content"] for b in log["bodies"]))
check("people layout: log counts the ties", any("people: 3 cards in the fixed layout" in m and "2 backed by a quote" in m for m in LOGS), " | ".join(m for m in LOGS if "people:" in m)[:200])
per2 = {"Gabriel Bagradian": pe("Gabriel Bagradian", "", "", [("husband", "Juliette", Q_WIFE)]), "Stephan Bagradian": pe("Stephan Bagradian", "", "", [("son", "Gabriel", "Gabriel gave his son Stephan an order. Stephan nodded.")])}
w2 = trio(); w2[2]["excerpts"] = ["Gabriel gave his son Stephan an order. Stephan nodded. " + "x" * 80]
res, _ = run(lambda b, l: None, w2, entries_fn=world(per2), people_layout="yes", model_bulk="m", model_high="hi", parallel=1)
check("people layout: a son's own claim gives the father a gendered tie and Stephan 'Son of'", res["Stephan Bagradian"].description == "Son of Gabriel Bagradian." and res["Gabriel Bagradian"].description == "Husband of Juliette Bagradian, father of Stephan Bagradian.", f"{res['Stephan Bagradian'].description} | {res['Gabriel Bagradian'].description}")
bad = [("father", "Stephan", "Gabriel Bagradian was an Ottoman officer in the reserve."), ("father", "Stephan", "This sentence is nowhere in the excerpts: his son Stephan."),
       ("father", "Nobody", Q_SON), ("father", "Gabriel Bagradian", Q_SON), ("cousin-twice-removed", "Stephan", Q_SON), ("husband", "Juliette", Q_WIFE)]
res, _ = run(lambda b, l: None, trio(), entries_fn=world({"Gabriel Bagradian": pe("Gabriel Bagradian", "Officer", "", bad)}), people_layout="yes", model_bulk="m", model_high="hi", parallel=1)
lg = " | ".join(m for m in LOGS if "people:" in m)
check("ties are checked: a sentence with no family word, a quote not in the excerpts, a person not in the excerpts, the person themselves and an unknown relation are all rejected", res["Gabriel Bagradian"].description.startswith("Husband of Juliette Bagradian. Officer") and "1 backed by a quote" in lg and "1 quote not in the excerpts" in lg and "1 no family word" in lg and "1 other person not in the excerpts" in lg and "1 the person themselves" in lg and "1 unknown relation" in lg, lg[:330])
fam = {"A One": pe("A One", "", "", [("brother", "B Two", "A One and his brother B Two ran.")]), "B Two": pe("B Two", "", "", [("son", "A One", "B Two, the son of A One, ran.")])}
res, _ = run(lambda b, l: None, [pent("A One", ["A One and his brother B Two ran. " + "x" * 90], 50), pent("B Two", ["B Two, the son of A One, ran. " + "x" * 90], 40)], entries_fn=world(dict(fam, **{"A One": dict(fam["A One"], progress="Runs."), "B Two": dict(fam["B Two"], progress="Runs.")})), people_layout="yes", model_bulk="m", model_high="hi", parallel=1)
check("a pair that is told two different ways (brother / son) loses the tie on both cards", res["A One"].description == "Runs." and res["B Two"].description == "Runs." and any("dropped because the sentences split evenly" in m for m in LOGS), " | ".join(m for m in LOGS if "people:" in m)[-170:])
# the tie appears on the page from the point the book states it
exs = [f"ex{k} Gabriel Bagradian and Stephan walked. " + "x" * 60 for k in range(8)]
exs[5] = "ex5 Gabriel gave his son Stephan an order: report twice a day. " + "x" * 60
def paged(names, prompt):
    if "Write the cards of these people" not in prompt: return [{"original_name": n, "canonical_name": n, "description": "t"} for n in names]
    out = []
    for n in names:
        rels = [("father", "Stephan", "Gabriel gave his son Stephan an order: report twice a day.")] if ("ex5 " in prompt and n == "Gabriel Bagradian") else []
        out.append(pe(n, "Officer", f"Progress {'late' if 'ex5 ' in prompt else 'early'}.", rels))
    return out
enrich.TIES_FROM_START = False        # gating itself is under test here (--ties-from-start no)
res, _ = run(lambda b, l: None, [pent("Gabriel Bagradian", exs, 1000), pent("Stephan Bagradian", ["Stephan nodded. " + "x" * 100], 500)], entries_fn=paged, people_layout="yes", page_aware="yes", model_bulk="m", model_high="hi", parallel=1)
pg = res["Gabriel Bagradian"].stages
check("page-aware: pages before the tie is stated carry no tie, the pages after it do (position gating)", len(pg) >= 2 and not pg[0][0].startswith("Father") and pg[-1][0].startswith("Father of Stephan Bagradian."), str([(c[:40], p_) for c, p_ in pg]))
enrich.TIES_FROM_START = True
ps_ = res["Stephan Bagradian"].stages
check("page-aware: the other person's card gets the tie from the same point (before it: none; after it: 'Child of Gabriel Bagradian')", len(ps_) == 2 and not ps_[0][0].startswith("Child") and ps_[1][0].startswith("Child of Gabriel Bagradian.") and ps_[1][1] == 500, str([(c[:30], f) for c, f in ps_]))
# professions
dr = {"Gabriel Bagradian": pe("Gabriel Bagradian", "Doctor and head of the household", "Leads the defense.")}
bookNo = enrich.norm_text("gabriel bagradian said to the doctor: come. gabriel bagradian was an ottoman officer.")
res, _ = run(lambda b, l: None, [pent("Gabriel Bagradian", G_EX, 1000)], entries_fn=world(dr), people_layout="yes", book=bookNo, model_bulk="m", model_high="hi", parallel=1)
check("professions: 'doctor' is dropped when the book never attaches it to the person (a doctor in the same scene is not enough)", res["Gabriel Bagradian"].description == "Leads the defense." and any("1 sentences dropped for a profession" in m for m in LOGS), res["Gabriel Bagradian"].description)
bookYes = enrich.norm_text("Gabriel Bagradian was a doctor of law.")
res, _ = run(lambda b, l: None, [pent("Gabriel Bagradian", G_EX, 1000)], entries_fn=world({"Gabriel Bagradian": pe("Gabriel Bagradian", "Doctor of law", "Leads.")}), people_layout="yes", book=bookYes, model_bulk="m", model_high="hi", parallel=1)
check("professions: kept when the book says it of this person ('Gabriel Bagradian was a doctor of law')", res["Gabriel Bagradian"].description == "Doctor of law. Leads.", res["Gabriel Bagradian"].description)
check("professions: apposition counts (Ter Haigasun, head priest) and title-before-name (the pastor Aram)", enrich.profession_attached("priest", ["haigasun"], "ter haigasun, head priest of yoghonoluk") and enrich.profession_attached("pastor", ["aram"], "the pastor aram tomasian") and not enrich.profession_attached("doctor", ["bagradian"], "bagradian said to the doctor"))
res, _ = run(lambda b, l: None, [pent("Gabriel Bagradian", G_EX, 1000)], entries_fn=world({"Gabriel Bagradian": pe("Gabriel Bagradian", " ".join(["word"] * 40), " ".join(["word"] * 70) + ".")}), people_layout="yes", model_bulk="m", model_high="hi", parallel=1)
check("length limits: a role over 25 words and a one-sentence progress over 45 words are ignored", "Gabriel Bagradian" not in res)
res, _ = run(lambda b, l: None, [pent("Gabriel Bagradian", G_EX, 1000)], entries_fn=world({"Gabriel Bagradian": pe("Gabriel Bagradian", "Officer", "He fights well. He was killed in the end.")}), people_layout="yes", model_bulk="m", model_high="hi", parallel=1)
check("the spoiler guard applies to role and progress too (static: an outcome sentence is removed)", res["Gabriel Bagradian"].description == "Officer. He fights well.", res["Gabriel Bagradian"].description)
res, _ = run(lambda b, l: None, [pent("Gabriel Bagradian", G_EX, 1000), pent("Some Place", ["A place. " + "x" * 100], 50, typ=2)], entries_fn=world({"Gabriel Bagradian": pe("Gabriel Bagradian", "Officer", "Leads.")}), people_layout="yes", model_bulk="m", model_high="hi", parallel=1)
check("terms (places, events) keep the free-text card and are not given ties", res["Some Place"].description == "A term card." and res["Gabriel Bagradian"].description == "Officer. Leads.")
seen_prompts = []
def two_stage(names, prompt):
    seen_prompts.append(prompt)
    return world({"Gabriel Bagradian": pe("Gabriel Bagradian", "Officer", "Leads.", [("husband", "Juliette", Q_WIFE)])})(names, prompt)
exs4 = [G_EX[0] + " " + "x" * 50, G_EX[1] + " " + "x" * 50, "third excerpt Gabriel " + "x" * 50, "fourth excerpt Gabriel " + "x" * 50]
res, _ = run(lambda b, l: None, [pent("Gabriel Bagradian", exs4, 1000), pent("Juliette Bagradian", ["Juliette smiled. " + "x" * 100], 600)], entries_fn=two_stage, people_layout="yes", model_bulk="m", model_high="hi", parallel=1)
check("later stages show 'KNOWN SO FAR' (role, progress, ties recorded); the first stage does not", "KNOWN SO FAR" not in seen_prompts[0] and any("KNOWN SO FAR: role: Officer | progress: Leads. | ties recorded: husband of Juliette" in pr for pr in seen_prompts[1:]), str(len(seen_prompts)))
check("render_ties: closest first, one tie per person, grouped, at most four people", enrich.render_ties([("friend", "Z"), ("son", "A"), ("husband", "W"), ("son", "B"), ("father", "C"), ("rival", "Y"), ("brother", "D")]) == "Husband of W, father of C, son of A and B.")
check("assemble_person_card: the word ceiling trims progress, never the ties or the role", enrich.assemble_person_card([("father", "S")], "Officer", " ".join(["Word."] * 120)).startswith("Father of S. Officer.") and enrich.word_count(enrich.assemble_person_card([("father", "S")], "Officer", " ".join(["Word."] * 120))) <= 90)
check("resolve_other: a first name goes to the label with by far the most mentions; ambiguity gives None", enrich.resolve_other("Stephan", {"Stephan Bagradian": 591, "Young Stephan": 2}) == "Stephan Bagradian" and enrich.resolve_other("Stephan", {"Stephan Bagradian": 100, "Stephan Haik": 90}) is None and enrich.resolve_other("Nobody", {"Stephan Bagradian": 5}) is None)

# ---- v1.2.11: Wikipedia fact check (a fake Wikipedia: no network) --------------------------------------------------
import tempfile as _tf
REF = ("== Characters ==\nGabriel Bagradian - An Armenian from the local village who was raised in France.\n"
       "Juliette Bagradian - Gabriel's French wife.\nStephan Bagradian - Gabriel's young nephew who defends the village.\n")
def wiki_world(per, verdicts):
    """per: people entries; verdicts: label -> {claim number: (verdict, evidence)}; every other claim is not_covered"""
    people = world(per)
    seen_checks = []
    def f(names, prompt):
        if "Claims about" in prompt:
            label = re.search(r"Claims about (.+?), taken from the book:", prompt).group(1)
            seen_checks.append((label, prompt))
            n = len(re.findall(r"^\d+\. ", prompt, re.M))
            v = verdicts.get(label, {})
            return [{"claim": i, "verdict": v.get(i, ("not_covered", ""))[0], "evidence": v.get(i, ("not_covered", ""))[1]} for i in range(1, n + 1)]
        return people(names, prompt)
    f.seen = seen_checks
    return f
per_w = {"Gabriel Bagradian": pe("Gabriel Bagradian", "Armenian reserve officer", "Leads the defense.", [("husband", "Juliette", Q_WIFE), ("father", "Stephan", Q_SON)])}
fetched = []
def fake_fetch(title):
    fetched.append(title)
    return (REF, title) if title in ("The Boo", "Gabriel Bagradian") else None
def wrun(verdicts, per=per_w, **kw):
    fx = wiki_world(per, verdicts)
    res, log = run(lambda b, l: None, trio(), entries_fn=fx, people_layout="yes", wiki_check=kw.pop("wiki_check", "yes"),
                   wiki_fetch=kw.pop("wiki_fetch", fake_fetch), title="The Boo", wiki_pages=["Gabriel Bagradian"], model_bulk="m", model_high="hi", parallel=1, **kw)
    return res, log, fx
fetched.clear()
res, log, fx = wrun({"Gabriel Bagradian": {2: ("contradicted", "Stephan Bagradian - Gabriel's young nephew who defends the village."), 1: ("supported", "Juliette Bagradian - Gabriel's French wife.")}})
check("wiki: a tie the reference contradicts (father vs 'nephew') is removed, and so is the card the other person would have got", res["Gabriel Bagradian"].description == "Husband of Juliette Bagradian. Armenian reserve officer. Leads the defense." and "Stephan Bagradian" not in res, res["Gabriel Bagradian"].description)
check("wiki: the supported tie stays; the log counts supported and removed claims", "Wife of Gabriel Bagradian." == res["Juliette Bagradian"].description and any("1 claims supported" in m and "1 ties and 0 roles removed" in m for m in LOGS), " | ".join(m for m in LOGS if "Wikipedia" in m)[:200])
check("wiki: the book's page and the fictional characters' own pages are fetched, by title only", fetched[:2] == ["The Boo", "Gabriel Bagradian"], str(fetched))
_lines = ["Gabriel Bagradian - An Armenian from the local village who was raised in France.", "Stephan Bagradian - Gabriel's young nephew who defends the village."]
_meta = [{"kind": "tie", "other": "Stephan"}]
check("wiki: a contradiction without the reference sentence quoted exactly is ignored", enrich.accept_verdicts([{"claim": 1, "verdict": "contradicted", "evidence": "A sentence that the reference does not contain at all."}], _meta, _lines, "Gabriel Bagradian") == {})
check("wiki: a contradiction whose sentence names neither the person nor the other person of the claim is ignored", enrich.accept_verdicts([{"claim": 1, "verdict": "contradicted", "evidence": "An Armenian from the local village who was raised in France."}], [{"kind": "tie", "other": "Zed"}], _lines, "Ziggy") == {})
check("wiki: a verdict with the exact sentence counts, and returns the sentence", enrich.accept_verdicts([{"claim": 1, "verdict": "contradicted", "evidence": "Stephan Bagradian - Gabriel's young nephew who defends the village."}], _meta, _lines, "Gabriel Bagradian") == {1: ("contradicted", "Stephan Bagradian - Gabriel's young nephew who defends the village.")})
res, _, _ = wrun({"Gabriel Bagradian": {1: ("supported", "Juliette Bagradian - Gabriel's French wife."), 2: ("supported", "Stephan Bagradian - Gabriel's young nephew who defends the village."), 3: ("contradicted", "Gabriel Bagradian - An Armenian from the local village who was raised in France.")}})
check("wiki: a role the reference contradicts is removed from the card", res["Gabriel Bagradian"].description == "Husband of Juliette Bagradian, father of Stephan Bagradian. Leads the defense." and any("1 roles removed" in m for m in LOGS), res["Gabriel Bagradian"].description)
res, _, fx = wrun({})
check("wiki: only people with claims and reference lines are asked about (no request for Juliette or Stephan, who have no claims of their own)", [l for l, _p in fx.seen] == ["Gabriel Bagradian"], str([l for l, _p in fx.seen]))
check("wiki: the request carries the reference lines and the numbered claims, and asks for verbatim evidence", "Gabriel Bagradian - An Armenian from the local village" in fx.seen[0][1] and "1. Gabriel Bagradian is the husband of Juliette." in fx.seen[0][1])
fetched.clear()
res, log, fx = wrun({}, wiki_fetch=lambda t: None)
check("wiki: no page text (offline) -> the check is skipped, the cards are exactly as without it", res["Gabriel Bagradian"].description.startswith("Husband of Juliette Bagradian, father of Stephan Bagradian.") and any("fact check skipped" in m for m in LOGS))
fetched.clear()
res, log, fx = wrun({}, wiki_check="no")
check("wiki: switched off -> nothing is fetched and no check request is made", fetched == [] and fx.seen == [])
sents_ = enrich.split_reference("== H ==\nShort.\nGabriel Bagradian - An Armenian from the local village. Juliette Bagradian - Gabriel's French wife.\n")
check("wiki: split_reference drops headings and short fragments and keeps list lines as sentences", sents_ == ["Gabriel Bagradian - An Armenian from the local village.", "Juliette Bagradian - Gabriel's French wife."], str(sents_))
check("wiki: rank_reference puts the sentences with the most of the name words first", enrich.rank_reference(["gabriel", "juliette"], ["Nothing relevant here at all, really.", "Gabriel went out alone today.", "Juliette Bagradian - Gabriel's French wife."])[0].startswith("Juliette Bagradian"))
cd_ = Path(_tf.mkdtemp())
(cd_ / "The_Boo.json").write_text(json.dumps({"title": "The Boo", "text": "cached text of the page"}), encoding="utf-8")
check("wiki: a fresh cache file is used without any network request", enrich.wiki_fetch_http("The Boo", cache_dir=cd_) == ("cached text of the page", "The Boo"))

# ---- v1.2.11: ties found in the book's own sentences ---------------------------------------------------------------
BOOK = ("Gabriel gave his son Stephan an order. The mountain was cold. Stephan walked alone. "
        "Gabriel spoke with Minor Guy about the weather. Gabriel and his wife Juliette waited. Juliette's son Minor Guy slept.")
def mention(word, nth=0):
    i = -1
    for _ in range(nth + 1): i = BOOK.find(word, i + 1)
    return (i, len(word))
wk = [{"id": 1, "name": "Gabriel Bagradian", "type": 1, "count": 100}, {"id": 2, "name": "Stephan Bagradian", "type": 1, "count": 60},
      {"id": 3, "name": "Minor Guy", "type": 1, "count": 5}, {"id": 4, "name": "Juliette Bagradian", "type": 1, "count": 80}]
ocs = {1: [mention("Gabriel", 0), mention("Gabriel", 1), mention("Gabriel", 2)], 2: [mention("Stephan", 0), mention("Stephan", 1)],
       3: [mention("Minor Guy", 0), mention("Minor Guy", 1)], 4: [mention("Juliette", 0), mention("Juliette", 1)]}
cands_ = enrich.find_tie_candidates(wk, ocs, [0], [BOOK], window=1)      # one sentence: the 1.2.11 scan; the wider window has its own tests
check("tie scan: a sentence with a family word and two main people is a candidate; one person, no family word, or a minor entry is not", [c["sentence"] for c in cands_] == ["Gabriel gave his son Stephan an order.", "Gabriel and his wife Juliette waited."], str([c["sentence"] for c in cands_]))
check("tie scan: each candidate has its position and the people, most-mentioned first", cands_[0]["pos"] == 0 and cands_[0]["names"] == ["Gabriel Bagradian", "Stephan Bagradian"] and cands_[1]["names"] == ["Gabriel Bagradian", "Juliette Bagradian"])
def tie_world(per, answers):
    people = world(per)
    seen = []
    def f(names, prompt):
        if "Numbered items. Say how A is related to B" in prompt:
            seen.append(prompt)
            n = len(re.findall(r"^\d+\. A = ", prompt, re.M))
            return [{"id": i, "relation": answers.get(i, "none")} for i in range(1, n + 1)]
        return people(names, prompt)
    f.seen = seen
    return f
cc = [{"pos": 100, "sentence": "Gabriel gave his son Stephan an order.", "names": ["Gabriel Bagradian", "Stephan Bagradian"]},
      {"pos": 200, "sentence": "My son, said the priest to Gabriel.", "names": ["Ter Haigasun", "Gabriel Bagradian"]}]
tw = trio() + [pent("Ter Haigasun", ["The priest spoke. " + "x" * 100], 400)]
fx = tie_world({"Gabriel Bagradian": pe("Gabriel Bagradian", "Officer", "Leads.")}, {1: "father", 2: "none"})
res, _ = run(lambda b, l: None, tw, entries_fn=fx, people_layout="yes", tie_candidates=cc, model_bulk="m", model_high="hi", parallel=1)
check("tie pass: the model's classification of a sentence becomes a tie on both cards; 'none' (a priest's 'my son') adds nothing", res["Gabriel Bagradian"].description == "Father of Stephan Bagradian. Officer. Leads." and res["Stephan Bagradian"].description == "Child of Gabriel Bagradian." and "Ter Haigasun" not in res, f"{res['Gabriel Bagradian'].description} | {res.get('Stephan Bagradian') and res['Stephan Bagradian'].description}")
check("tie pass: the request names both people and the sentence; the log counts sentences and pairs", len(fx.seen) == 1 and "Gabriel gave his son Stephan an order." in fx.seen[0] and any("tie scan: 2 sentences" in m and "1 of 2 pairs" in m for m in LOGS), " | ".join(m for m in LOGS if "tie scan" in m))
res, _ = run(lambda b, l: None, tw, entries_fn=tie_world({}, {1: "cousin-twice"}), people_layout="yes", tie_candidates=cc, model_bulk="m", model_high="hi", parallel=1)
check("tie pass: a relation outside the list is ignored (nothing is written at all)", isinstance(res, str) and res.startswith("SKIP"), str(res)[:80])
w_raw = [pent("Gabriel Bagradian", ["Gabriel gave his son Stephan an order. " + "x" * 100], 1000), pent("Stephan Sets Out", ["Stephan nodded. " + "x" * 100], 590)]
res, _ = run(lambda b, l: None, w_raw, entries_fn=tie_world({"Gabriel Bagradian": pe("Gabriel Bagradian", "Officer", ""), "Stephan Sets Out": dict(pe("Stephan Sets Out", "", "Nods."), canonical_name="Stephan Bagradian")}, {1: "father"}), people_layout="yes",
              tie_candidates=[{"pos": 0, "sentence": "Gabriel gave his son Stephan an order.", "names": ["Gabriel Bagradian", "Stephan Sets Out"]}], model_bulk="m", model_high="hi", parallel=1)
check("tie pass: the other person is found by the entity, so a chapter-title label ('Stephan Sets Out') still ends up as 'Stephan Bagradian'", res["Gabriel Bagradian"].description.startswith("Father of Stephan Bagradian."), res["Gabriel Bagradian"].description)
pp, cn = enrich.build_people_cards({"A One": {"label": "A One", "count": 100, "rels": [{"rel": "father", "other": "Ces", "other_key": "Ces", "pos": 0}], "rp": [("Officer", "Leads.", 0)]},
                                    "Ces": {"label": "Ces", "count": 17, "rels": [], "rp": [("", "Says things.", 0)]}})
check("minor entries: nobody gets a tie to an entry with fewer than 20 mentions ('Child of Papa'); the minor entry itself may still show its tie", "Ces" not in pp["A One"][1] and cn["minor"] >= 1 and pp["Ces"][1].startswith("Child of A One"), f"{pp['A One'][1]} | {pp['Ces'][1]} | {cn}")
enrich.TIES_FROM_START = False        # these two tests are about the gating itself (the 1.2.11 behaviour, --ties-from-start no)
rp_ = [("Officer", "Early.", 0), ("Officer", "Late.", 300)]
pp, _ = enrich.build_people_cards({"A One": {"label": "A One", "count": 100, "rels": [{"rel": "father", "other": "B Two", "other_key": "B Two", "pos": 150}], "rp": rp_},
                                   "B Two": {"label": "B Two", "count": 100, "rels": [], "rp": [("Boy", "Plays.", 0)]}}, gate=True)
pages_ = pp["A One"][0]
check("page-aware: a page starts where the tie is first stated, not only at stage boundaries (0: none, 150: tie, 300: tie + later progress)", [(c, f) for c, f in pages_] == [("Officer. Early.", None), ("Father of B Two. Officer. Early.", 150), ("Father of B Two. Officer. Late.", 300)], str(pages_))
enrich.TIES_FROM_START = True
role_world = {"Gabriel Bagradian": pe("Gabriel Bagradian", "Head of the Bagradian family; leader of the resistance; sister of Juliette", "Leads the defense.", [("husband", "Juliette", Q_WIFE)])}
res, log, fx = wrun({"Gabriel Bagradian": {2: ("contradicted", "Gabriel Bagradian - An Armenian from the local village who was raised in France.")}}, per=role_world)
check("wiki roles: only the parts that make a family or job claim are checked, one by one", "leader of the resistance" not in fx.seen[0][1] and "Bagradian family" not in fx.seen[0][1] and "sister of Juliette" in fx.seen[0][1], fx.seen[0][1][-260:])
check("wiki roles: a contradicted part is removed and the rest of the role stays", "sister of Juliette" not in res["Gabriel Bagradian"].description and "Head of the Bagradian family; leader of the resistance." in res["Gabriel Bagradian"].description, res["Gabriel Bagradian"].description)

# ---- v1.2.11 (d): in-laws, majority rule, role vetoes that must be about the person ---------------------------------
chk = lambda people, **kw: enrich.build_people_cards(people, **kw)
IL = {"Iskuhi T": {"label": "Iskuhi T", "count": 100, "rels": [{"rel": "sister-in-law", "other": "Hovsannah T", "other_key": "Hovsannah T", "pos": 0}], "rp": [("Teacher", "Helps.", 0)]},
      "Hovsannah T": {"label": "Hovsannah T", "count": 90, "rels": [], "rp": [("Wife", "Waits.", 0)]}}
pp, cn = chk(IL)
check("in-laws: 'sister-in-law' is a relation; the other person gets 'sibling-in-law' back while her own gender is unknown", pp["Iskuhi T"][1].startswith("Sister-in-law of Hovsannah T.") and pp["Hovsannah T"][1].startswith("Sibling-in-law of Iskuhi T."), f"{pp['Iskuhi T'][1]} | {pp['Hovsannah T'][1]}")
IL["Hovsannah T"]["rels"] = [{"rel": "wife", "other": "Aram T", "other_key": "Aram T", "pos": 0}]
IL["Aram T"] = {"label": "Aram T", "count": 80, "rels": [], "rp": [("Pastor", "Preaches.", 0)]}
pp, cn = chk(IL)
check("in-laws: once she is known to be a woman (she is somebody's wife) the tie comes back as 'sister-in-law'", pp["Hovsannah T"][1].startswith("Wife of Aram T") and "sister-in-law of Iskuhi T" in pp["Hovsannah T"][1], pp["Hovsannah T"][1])
check("in-laws: the hyphenated word counts as a family word in a quote, and the scan offers it to the model", enrich.valid_tie("sister-in-law", "Hovsannah", "Iskuhi and her sister-in-law Hovsannah ran.", "Iskuhi T", ["Iskuhi and her sister-in-law Hovsannah ran."], [0])[0] and "sister-in-law" in enrich.TIES_SYSTEM)
MJ = {"Gab": {"label": "Gab", "count": 100, "rels": [{"rel": "father", "other": "Ste", "other_key": "Ste", "pos": 0, "n": 4}, {"rel": "son", "other": "Ste", "other_key": "Ste", "pos": 0, "n": 1}], "rp": [("Officer", "Leads.", 0)]},
      "Ste": {"label": "Ste", "count": 100, "rels": [], "rp": [("Boy", "Plays.", 0)]}}
pp, cn = chk(MJ)
check("majority: father (4 sentences) against son (1) between the same two people -> father stays, son goes; counted as settled", pp["Gab"][1].startswith("Father of Ste.") and "Son of" not in pp["Gab"][1] and cn["resolved"] >= 1 and cn["conflicts"] == 0, f"{pp['Gab'][1]} | {cn}")
MJ["Gab"]["rels"][0]["n"] = 1
pp, cn = chk(MJ)
check("majority: an even split drops both", not pp["Gab"][1].startswith(("Father", "Son")) and cn["conflicts"] >= 1, f"{pp['Gab'][1]} | {cn}")
res, _ = run(lambda b, l: None, tw, entries_fn=tie_world({"Gabriel Bagradian": pe("Gabriel Bagradian", "Officer", "Leads.")}, {1: "father"}), people_layout="yes",
             tie_candidates=cc + [{"pos": 300, "sentence": "Gabriel gave his son Stephan another order.", "names": ["Gabriel Bagradian", "Stephan Bagradian"]}], model_bulk="m", model_high="hi", parallel=1)
check("a second sentence that says the same tie counts as support (n = 2), not as a new tie", res["Gabriel Bagradian"].description.startswith("Father of Stephan Bagradian.") and res["Gabriel Bagradian"].description.count("Stephan") == 1)
check("the scan is told that both must be people (a 'son of Zeitun' is a native of a town)", "place" in enrich.TIES_SYSTEM and "Zeitun" in enrich.TIES_SYSTEM)
RW = {"Ter Haigasun": pe("Ter Haigasun", "District head superintendent of schools and priest", "Leads the people.")}
def ter_world(verdict_evidence):
    people = world(RW)
    seen = []
    def f(names, prompt):
        if "Claims about" in prompt:
            seen.append(prompt)
            n = len(re.findall(r"^\d+\. ", prompt, re.M))
            return [{"claim": i, "verdict": "contradicted", "evidence": verdict_evidence} for i in range(1, n + 1)]
        return people(names, prompt)
    f.seen = seen
    return f
REF2 = "== Characters ==\nHrand Oskanian - School teacher in conflict with Ter Haigasun\nTer Haigasun - Gregorian head priest who convinces the villagers to resist.\n"
tw2 = [pent("Ter Haigasun", ["Ter Haigasun was the priest of the village. The priest spoke. " + "x" * 100], 400)]
for ev, expect_kept in (("Hrand Oskanian - School teacher in conflict with Ter Haigasun", True), ("Ter Haigasun - Gregorian head priest who convinces the villagers to resist.", False)):
    fx = ter_world(ev)
    res, _ = run(lambda b, l: None, tw2, entries_fn=fx, people_layout="yes", wiki_check="yes", wiki_fetch=lambda t: (REF2, t), title="The Boo", wiki_pages=[], model_bulk="m", model_high="hi", parallel=1,
                 book=enrich.norm_text("ter haigasun, the priest of the village. superintendent of schools"))
    kept = "priest" in res["Ter Haigasun"].description.lower()
    check(f"wiki roles: the veto sentence {'about someone else (Hrand Oskanian ... conflict with Ter Haigasun) is ignored' if expect_kept else 'about the person (Ter Haigasun - ...) counts'}", kept == expect_kept, res["Ter Haigasun"].description)

# ---- v1.2.11 (e): gender from the card text, single-sentence ties must be confirmed, the role does not repeat a tie ----
pp, _ = chk({"Gab": {"label": "Gab", "count": 100, "rels": [{"rel": "father", "other": "Ste", "other_key": "Ste", "pos": 0}], "rp": [("Officer", "He leads. His men follow him.", 0)]},
             "Ste": {"label": "Ste", "count": 100, "rels": [], "rp": [("Boy", "He wears clothes. He proves himself to Haik.", 0)]}})
check("gender from the pronouns of the person's own card text: 'He wears ...' -> 'Son of', not 'Child of'", pp["Ste"][1].startswith("Son of Gab."), pp["Ste"][1])
pp, _ = chk({"Gab": {"label": "Gab", "count": 100, "rels": [{"rel": "father", "other": "Ste", "other_key": "Ste", "pos": 0}], "rp": [("Officer", "He leads.", 0)]},
             "Ste": {"label": "Ste", "count": 100, "rels": [], "rp": [("Girl", "She wears clothes. She proves herself to Haik.", 0)]}})
check("gender from pronouns: 'She ...' -> 'Daughter of'", pp["Ste"][1].startswith("Daughter of Gab."), pp["Ste"][1])
pp, _ = chk({"Gab": {"label": "Gab", "count": 100, "rels": [{"rel": "father", "other": "Ste", "other_key": "Ste", "pos": 0}], "rp": [("Officer", "He leads.", 0)]},
             "Ste": {"label": "Ste", "count": 100, "rels": [], "rp": [("Child", "He is here. She is there. It rains.", 0)]}})
check("gender from pronouns: a mixed or thin signal stays 'Child of'", pp["Ste"][1].startswith("Child of Gab."), pp["Ste"][1])
check("the role does not repeat the ties line (\"Gabriel's wife\" next to \"Wife of Gabriel\"), the rest of the role stays", enrich.assemble_person_card([("wife", "Gabriel Bagradian")], "Gabriel's wife; prefers croquet", "Waits.") == "Wife of Gabriel Bagradian. Prefers croquet. Waits.", enrich.assemble_person_card([("wife", "Gabriel Bagradian")], "Gabriel's wife; prefers croquet", "Waits."))
check("the role keeps a part that only mentions a name, without a family word", enrich.assemble_person_card([("wife", "Gabriel Bagradian")], "Friend of Gabriel", "Waits.") == "Wife of Gabriel Bagradian. Friend of Gabriel. Waits.")
# a tie from one sentence is confirmed by the reference or it goes; two sentences keep it
def both(per, answers, verdicts):
    base = wiki_world(per, verdicts); ties = tie_world(per, answers)
    def f(names, prompt):
        if "Numbered items. Say how A is related to B" in prompt: return ties(names, prompt)
        return base(names, prompt)
    f.seen = base.seen
    return f
pw = {"Gabriel Bagradian": pe("Gabriel Bagradian", "Officer", "Leads.")}
cands1 = [{"pos": 100, "sentence": "Gabriel gave his son Stephan an order.", "names": ["Gabriel Bagradian", "Stephan Bagradian"]}]
res, _ = run(lambda b, l: None, trio(), entries_fn=both(pw, {1: "father"}, {}), people_layout="yes", tie_candidates=cands1, wiki_check="yes", wiki_fetch=fake_fetch, title="The Boo", wiki_pages=["Gabriel Bagradian"], model_bulk="m", model_high="hi", parallel=1)
check("single-sentence tie not confirmed by the reference (the reference has lines about Gabriel but none says it) -> removed, counted in the log", not res["Gabriel Bagradian"].description.startswith("Father") and any("1 ties from a single sentence removed" in m for m in LOGS), res["Gabriel Bagradian"].description + " | " + " | ".join(m for m in LOGS if "Wikipedia fact" in m)[:160])
res, _ = run(lambda b, l: None, trio(), entries_fn=both(pw, {1: "father"}, {"Gabriel Bagradian": {1: ("supported", "Stephan Bagradian - Gabriel's young nephew who defends the village.")}}), people_layout="yes", tie_candidates=cands1, wiki_check="yes", wiki_fetch=fake_fetch, title="The Boo", wiki_pages=["Gabriel Bagradian"], model_bulk="m", model_high="hi", parallel=1)
check("single-sentence tie the reference confirms stays", res["Gabriel Bagradian"].description.startswith("Father of Stephan Bagradian."), res["Gabriel Bagradian"].description)
cands2 = cands1 + [{"pos": 200, "sentence": "Gabriel gave his son Stephan a second order.", "names": ["Gabriel Bagradian", "Stephan Bagradian"]}]
res, _ = run(lambda b, l: None, trio(), entries_fn=both(pw, {1: "father", 2: "father"}, {}), people_layout="yes", tie_candidates=cands2, wiki_check="yes", wiki_fetch=fake_fetch, title="The Boo", wiki_pages=["Gabriel Bagradian"], model_bulk="m", model_high="hi", parallel=1)
check("a tie two sentences agree on stays even without a confirmation", res["Gabriel Bagradian"].description.startswith("Father of Stephan Bagradian."), res["Gabriel Bagradian"].description)

# ---- v1.2.11 (f): family claims only about tied people; a vague judge does not cost a true tie -----------------------
ca = enrich.assemble_person_card
check("a family claim in the progress about someone the card has no tie to is dropped (\"Stephan's wife\" on Hovsannah)", ca([("wife", "Aram Tomasian")], "", "Stephan's wife, near her time. She gives birth in silence.") == "Wife of Aram Tomasian. She gives birth in silence.", ca([("wife", "Aram Tomasian")], "", "Stephan's wife, near her time. She gives birth in silence."))
check("a family word about a tied person stays (\"grief for her son Stephan\" on Juliette)", ca([("mother", "Stephan Bagradian")], "", "She grieves for her son Stephan. She waits.") == "Mother of Stephan Bagradian. She grieves for her son Stephan. She waits.")
check("a role part that is a family claim about someone with no tie goes (\"Wife of Gabriel Bagradian\" on Iskuhi), the rest stays", ca([("sister", "Pastor Aram")], "Wife of Gabriel Bagradian; assistant teacher", "Helps.") == "Sister of Pastor Aram. Assistant teacher. Helps.", ca([("sister", "Pastor Aram")], "Wife of Gabriel Bagradian; assistant teacher", "Helps."))
check("with no ties at all every family claim in the role and progress goes, other sentences stay", ca([], "Gabriel's wife; poet", "She is Stephan's mother. She walks home.") == "Poet. She walks home.", ca([], "Gabriel's wife; poet", "She is Stephan's mother. She walks home."))
check("'family' and 'friends' are not family claims", ca([], "Head of the household", "He worries for his family. His friends gather.") == "Head of the household. He worries for his family. His friends gather.")
res, _ = run(lambda b, l: None, trio(), entries_fn=both(pw, {1: "father"}, {"Gabriel Bagradian": {1: ("supported", "A sentence the reference does not contain.")}}), people_layout="yes", tie_candidates=cands1, wiki_check="yes", wiki_fetch=fake_fetch, title="The Boo", wiki_pages=["Gabriel Bagradian"], model_bulk="m", model_high="hi", parallel=1)
check("a judge that says 'supported' but quotes badly does not cost a single-sentence tie", res["Gabriel Bagradian"].description.startswith("Father of Stephan Bagradian."), res["Gabriel Bagradian"].description)

print("\nALL PASSED" if not check.failed else f"\n{check.failed} FAILED"); sys.exit(1 if check.failed else 0)
