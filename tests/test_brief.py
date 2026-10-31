"""Tests of the 1.2.13 book brief (library metadata as context for the local model).
    python3 test_brief.py [dir containing enrich.py, cast_pass.py and book_brief.py]"""
import json, sys, threading, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent))
import book_brief as bb, cast_pass as cp, enrich

FAILED = 0
def check(label, ok, extra=""):
    global FAILED
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else "")); FAILED += (not ok)

BLURB = ("<div><p>It is 1915 and Gabriel has returned to his childhood home, an Armenian village on the slopes of Musa Dagh.</p>"
         "<p>The seminal novel of the Armenian genocide, Franz Werfel's bestselling 1933 epic brought the catastrophe to the world. " + "x " * 600 + "</p></div>")
musa = bb.make_brief("The Forty Days of Musa Dagh", ["Franz Werfel"], datetime.datetime(1933, 11, 1), "Penguin Modern Classics", "Penguin", ["eng"],
                     ["Historical", "War", "Classics", "Kindle", "unread"], BLURB)
check("make_brief: HTML stripped, blurb cut to about 700 characters, noise tags dropped, year from the date",
      "<" not in musa["blurb"] and 600 < len(musa["blurb"]) <= 710 and musa["blurb"].endswith("...") and musa["tags"] == ["Historical", "War", "Classics"]
      and musa["year"] == 1933 and musa["authors"] == ["Franz Werfel"], str(musa)[:200])
check("kind: 'Historical, War, Classics' alone is ambiguous, the blurb ('the seminal novel') says fiction", musa["kind"] == "fiction")
klg = bb.make_brief("King Leopold's Ghost", ["Adam Hochschild"], "1998-05-01", "", "", ["eng"], ["Politics", "History", "Biography"], "")
check("kind: 'Politics, History, Biography' is nonfiction", klg["kind"] == "nonfiction" and klg["year"] == 1998)
check("kind: conflicting tags stay unknown; no tags and no blurb give no brief at all",
      bb.kind_of({"tags": ["History", "Fantasy"], "blurb": ""}) == "unknown" and bb.make_brief("Untitled", [], None, "", "", [], [], "") == {})
check("kind: a blurb about a memoir is nonfiction", bb.kind_of({"tags": [], "blurb": "A memoir of growing up in Lagos."}) == "nonfiction")
full, short = bb.render(musa, blurb=True), bb.render(musa, blurb=False)
check("render: the full brief has the blurb, the short one does not; both say it is only context and list the tags and the kind",
      "Publisher's blurb" in full and "Publisher's blurb" not in short and "Kind of book: fiction" in short and "Historical, War, Classics" in short
      and "never from this note" in short and bb.render({}) == "")
p = Path(__import__("tempfile").mkdtemp()) / "b.json"; bb.dump(musa, p)
check("dump / load round trip; a missing or broken file gives an empty brief", bb.load(p) == musa and bb.load(p.parent / "none.json") == {} and bb.load(p.parent) == {})

seen = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); seen.append(body["messages"][1]["content"])
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps({"entries": [{"i": 0, "kind": "person", "wiki": "none"}]})}}], "usage": {}}).encode())
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
cl = cp.Client(f"http://127.0.0.1:{srv.server_port}/v1", "m"); cl.prefix = full
cl.ask(cp.TYPE_SYSTEM, "[0] Morel", cp.TYPE_SCHEMA)
cl2 = cp.Client(f"http://127.0.0.1:{srv.server_port}/v1", "m"); cl2.ask(cp.TYPE_SYSTEM, "[0] Morel", cp.TYPE_SCHEMA)
check("cast pass: the brief is put in front of the request when there is one, and the request is unchanged when there is none",
      seen[0].startswith("About this book") and seen[0].endswith("[0] Morel") and seen[1] == "[0] Morel", seen[0][:60])
srv.shutdown()
items = [{"a_show": "A", "b_show": "B", "sentence": "S.", "trigger": "S."}]
enrich.BRIEF_SHORT = short
with_brief = enrich.ties_prompt(items); enrich.BRIEF_SHORT = ""
check("tie scan: the short brief (no blurb) goes in front of the prompt; without a brief the prompt is the 1.2.12 one",
      with_brief.startswith("About this book") and "Publisher's blurb" not in with_brief and enrich.ties_prompt(items).startswith("Numbered items."))

# ---- the second check of every tie (1.2.13)
owner = {"rels": []}
t1 = {"rel": "husband", "other": "Mairik Antaram", "quote": "the doctor's lady"}
t2 = {"rel": "father", "other": "Stephan", "quote": "Gabriel gave his son Stephan an order"}
t3 = {"rel": "brother", "other": "Aram", "quote": "her brother Aram"}
owner["rels"] = [t1, t2, t3]
its = [{"owner": owner, "tie": t, "rel": t["rel"], "quote": t["quote"], "a_show": "A", "b_show": t["other"], "a": "A"} for t in (t1, t2, t3)]
gone = enrich.apply_verdicts(its, {1: "no", 2: "yes"})
check("tie check: only an explicit 'no' removes a tie; 'yes' and a missing answer keep it",
      [g["tie"]["other"] for g in gone] == ["Mairik Antaram"] and owner["rels"] == [t2, t3])
check("tie check: a broken answer (not 'no') removes nothing", enrich.apply_verdicts(its, {2: "maybe", 3: ""}) == [] and len(owner["rels"]) == 2)
vp = enrich.verify_prompt(its[:2])
check("tie check prompt: claim and quote for every item, numbered, nothing else needed",
      "1. Claim: A is the husband of Mairik Antaram." in vp and "Quote: the doctor's lady" in vp and "2. Claim: A is the father of Stephan." in vp)
check("tie check: the system prompt asks for the right direction, the exact relation and rejects hearsay and forms of address",
      all(w in enrich.VERIFY_SYSTEM for w in ("right direction", "sister-in-law", "form of address", "hearsay")) and enrich.VERIFY_SCHEMA["properties"]["entries"]["items"]["properties"]["verdict"]["enum"] == ["yes", "no"])
print("\nALL PASSED" if not FAILED else f"\n{FAILED} FAILED"); sys.exit(1 if FAILED else 0)
