"""Tests of the v1.2.12 cast pass: helpers, and the whole pass against a fake OpenAI-style server on synthetic books.
    python3 test_cast_pass.py [path/to/dir containing enrich.py and cast_pass.py]"""
import json, re, sqlite3, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent))
import cast_pass as cp
import enrich

FAILED = 0
def check(label, ok, extra=""):
    global FAILED
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    FAILED += (not ok)

# ---------------------------------------------------------------- helpers
check("proper_name: names yes, phrases/numbers/short no",
      all(cp.proper_name(x) for x in ["Úrsula", "Colonel Aureliano Buendía", "Santa Sofía de la Piedad", "King of the Belgians"])
      and not any(cp.proper_name(x) for x in ["the king", "EU", "1914", "x", "", "chapter one"]))
check("tokens_contain: first or last word(s) yes, the middle no",
      cp.tokens_contain("Aureliano Segundo", "Aureliano") and cp.tokens_contain("Henry Morton Stanley", "Stanley")
      and cp.tokens_contain("José Arcadio Buendía", "José Arcadio") and not cp.tokens_contain("José Arcadio Buendía", "Arcadio")
      and not cp.tokens_contain("Aureliano", "Aureliano"))
check("distinct_siblings: a title or numeral is the same person; an alias of a longer name is not a new sibling",
      cp.distinct_siblings("Leopold", ["King Leopold", "Leopold II", "Léopold II"]) == []
      and cp.distinct_siblings("Stanley", ["Henry Morton Stanley", "Henry Stanley"]) == ["Henry Morton Stanley"]
      and sorted(cp.distinct_siblings("Aureliano", ["Aureliano Segundo", "Aureliano José", "Aureliano Buendía", "Colonel Aureliano Buendía"]))
      == ["Aureliano Buendía", "Aureliano José", "Aureliano Segundo"])
check("distinct_siblings: two people with one first name stay two", len(cp.distinct_siblings("José Arcadio", ["José Arcadio Buendía", "José Arcadio Segundo"])) == 2)

T = "He met Ursula. Ursula said so. Amaranta Ursula came; Ursula's cat. King Leopold and the King went. Anatole France wrote of France."
check("find_spans: whole words only, not inside longer words", [T[s:e] for s, e in cp.find_spans(T, "Ursula")] == ["Ursula", "Ursula", "Ursula", "Ursula", "Ursula"][:len(cp.find_spans(T, "Ursula"))] and len(cp.find_spans(T, "Ursula")) == 4)
check("find_spans: a hyphen or apostrophe-joined word is not a match", cp.find_spans("Ursula-like and Ursulas", "Ursula") == [])
s0, e0 = cp.find_spans("Say Ursula, then.", "Ursula")[0]
check("selectable_len: name plus one following punctuation mark, as WordDumb highlights", cp.selectable_len("Say Ursula, then.", s0, e0) == 7 and cp.selectable_len("Say Ursula then.", s0, e0) == 6)
cl = cp.Claims([(10, 20), (30, 40)])
check("Claims: overlap test", cl.overlaps(15, 25) and cl.overlaps(5, 11) and not cl.overlaps(20, 30) and not cl.overlaps(0, 10) and cl.overlaps(35, 50))
cl.add(22, 28)
check("Claims.add keeps the test correct", cl.overlaps(24, 26) and not cl.overlaps(20, 22) and cl.overlaps(39, 45))
check("book_text: index equals the KFX position, gaps are NUL", cp.book_text([0, 20], ["abc", "xyz"])[20:23] == "xyz" and cp.book_text([0, 20], ["abc", "xyz"])[5] == "\0")
check("continuation_rate: 'King' is followed by names, 'Congo' is not", cp.continuation_rate("King Leopold and King Alfonso. King Peter.", cp.find_spans("King Leopold and King Alfonso. King Peter.", "King")) == 1.0
      and cp.continuation_rate("in Congo today", cp.find_spans("in Congo today", "Congo")) == 0.0)
t_out = {i: ("known", "X") for i in range(0, 40)}
t_out.update({i: ("new", "A") for i in range(60, 100)})
t_out[95_000] = ("known", "X")       # far from X's stretch, inside A's: moves to A
n_book = 100
tt = {p: v for p, v in t_out.items()}
tt2 = {p * 1000: v for p, v in tt.items() if p < 1000}
tt2[95_000] = ("known", "X")
ch = cp.settle_outliers(tt2, 100_000)
check("settle_outliers: a stray mention far from its character's stretch goes to the one character who covers it", ch == 1 and tt2[95_000] == ("new", "A"))
tt3 = {i * 1000: ("known", "X") for i in range(0, 40)}
tt3.update({i * 1000: ("new", "A") for i in range(60, 100)})
tt3[50_000] = ("known", "X")
tt3[99_900] = ("known", "X")
ch3 = cp.settle_outliers(tt3, 100_000)
check("settle_outliers: a mention near the other character's stretch but outside both is left alone only if it is close; far strays with no cover are dropped",
      99_900 not in tt3 or tt3[99_900] == ("new", "A"))
check("settle_outliers: needs two sizeable characters, otherwise nothing changes", cp.settle_outliers({i: ("known", "X") for i in range(30)}, 100) == 0)
check("trim_lonely_start: a stray first mention far before the rest is dropped, a close one is kept",
      cp.trim_lonely_start([630, 810, 820, 830, 850], 1000) == [810, 820, 830, 850]
      and cp.trim_lonely_start([430, 450, 510, 560, 580], 1000) == [430, 450, 510, 560, 580]
      and cp.trim_lonely_start([100, 500, 900], 1000) == [100, 500, 900])

check("sentence_at: the sentence around a position, whitespace tidied",
      cp.sentence_at("A first. The man (Ursula) walked on. Then more.", 20) == "The man (Ursula) walked on.")

# ---------------------------------------------------------------- the whole pass
FILL = "The afternoon wore on and nothing else happened in the house. "
def build_book():
    """-> text, marks (label -> [positions]); an early half and a late half."""
    parts, marks = [], {}
    pos = [0]
    def add(sentence, label=None, key=None):
        if label:
            off = sentence.index(key or label)
            marks.setdefault(label, []).append(pos[0] + off)
        parts.append(sentence); pos[0] += len(sentence)
    def filler(k):
        for _ in range(k): add(FILL)
    # early half: Ursula x6 (only 2 tagged by WordDumb), Amaranta Ursula x2, Aureliano Buendia (full) x12, bare Aureliano x14
    filler(20)
    for i in range(14): add(f"Then Ursula said something to the child, number {i}. ", "Ursula"); filler(6)
    for i in range(2): add(f"Amaranta Ursula came in, number {i}. ", "Amaranta Ursula"); filler(5)
    for i in range(14):
        if i < 12: add(f"Aureliano Buendia sat down, number {i}. ", "Aureliano Buendia"); filler(2)
        add(f"Aureliano looked up, number {i}. "); filler(3)
    filler(60)
    # late half: bare Aureliano x40, Aureliano Segundo (full) x12 (only in the middle), Morel x20, Ursula x4 more
    for i in range(12): add(f"Aureliano Segundo laughed, number {i}. ", "Aureliano Segundo"); filler(3)
    filler(60)
    for i in range(40): add(f"Aureliano read the parchments, number {i}. "); filler(5)
    for i in range(20): add(f"Edmund Morel wrote a letter to the editor, number {i}. ", "Morel"); filler(2)
    for i in range(4): add(f"Ursula wept again, number {i}. ", "Ursula"); filler(3)
    return "".join(parts), marks

BOOK, MARKS = build_book()
# fix the bare-Aureliano marks: recompute by search so the test does not depend on bookkeeping above
MARKS["Aureliano:early"] = [m.start() for m in re.finditer(r"Aureliano looked up", BOOK)]
MARKS["Aureliano:late"] = [m.start() for m in re.finditer(r"Aureliano read the parchments", BOOK)]
N = len(BOOK)

def make_db(path, wiki_for_morel=True):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE book_metadata (srl INTEGER, erl INTEGER, has_images TINYINT, has_excerpts TINYINT, show_spoilers_default TINYINT, num_people INTEGER, num_terms INTEGER, num_images INTEGER, preview_images TEXT);
    CREATE TABLE entity (id INTEGER PRIMARY KEY, label TEXT, loc_label INTEGER, type INTEGER, count INTEGER, has_info_card TINYINT);
    CREATE TABLE entity_description (text TEXT, source_wildcard TEXT, source INTEGER, entity INTEGER PRIMARY KEY);
    CREATE TABLE occurrence (entity INTEGER, start INTEGER, length INTEGER);
    CREATE TABLE type (id INTEGER PRIMARY KEY, label INTEGER, singular_label INTEGER, icon INTEGER, top_mentioned_entities TEXT);
    """)
    c.execute("INSERT INTO book_metadata VALUES (0, ?, 0, 0, 0, 0, 0, 0, '')", (N,))
    c.executemany("INSERT INTO type VALUES (?, 0, 0, 0, '')", [(1,), (2,)])
    def ent(i, label, typ, starts, length=None, source=None, desc="raw"):
        c.execute("INSERT INTO entity VALUES (?, ?, 7, ?, ?, 1)", (i, label, typ, len(starts)))
        c.execute("INSERT INTO entity_description VALUES (?, ?, ?, ?)", (desc, label, source, i))
        c.executemany("INSERT INTO occurrence VALUES (?, ?, ?)", [(i, s, (length or len(label))) for s in starts])
    # Ursula: a term, only the first 2 early and first 1 late mention tagged
    ur = MARKS["Ursula"]
    ent(1, "Ursula", 2, [ur[0], ur[1], ur[-1]])
    ent(2, "Amaranta Ursula", 2, MARKS["Amaranta Ursula"])
    ent(3, "Aureliano Buendia", 1, MARKS["Aureliano Buendia"])
    ent(4, "Aureliano Segundo", 1, MARKS["Aureliano Segundo"])
    bare = MARKS["Aureliano:early"] + MARKS["Aureliano:late"]
    ent(5, "Aureliano", 2, bare)                       # WordDumb's bare entry holding both eras
    ent(6, "Morel", 2, MARKS["Morel"], length=len("Morel"), source=1 if wiki_for_morel else None,
        desc="Morchella, the true morels, is a genus of edible sac fungi closely related to anatomically simpler cup fungi." if wiki_for_morel else "raw")
    c.commit(); c.close()

def make_server(typing=None, behaviour=None):
    """typing: label -> (kind, wiki); the assignment follows the position in the book."""
    typing = typing or {}
    log = {"calls": [], "kinds": []}
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            system, user = body["messages"][0]["content"], body["messages"][1]["content"]
            if behaviour and behaviour(system, user) == "boom":
                self.send_response(500); self.end_headers(); self.wfile.write(b"{}"); return
            if system.startswith("For each numbered name"):
                log["kinds"].append("type")
                labels = re.findall(r"^\[(\d+)\] (.+)$", user, re.M)
                out = [{"i": int(i), "kind": typing.get(l, ("other", "none"))[0], "wiki": typing.get(l, ("other", "none"))[1]} for i, l in labels]
            elif system.startswith("You build a character index"):
                log["kinds"].append("discover")
                out = [{"id": "A", "identity": "the man of the early chapters", "person": True, "same_as": "Aureliano Buendia", "from_pct": 0, "to_pct": 50},
                       {"id": "B", "identity": "the scholar of the late chapters", "person": True, "same_as": WRONG_SAME_AS[0], "from_pct": 50, "to_pct": 100}]
            else:
                log["kinds"].append("assign")
                out = []
                for i, pct in re.findall(r"^\[(\d+)\] \((\d+)% into the book\)", user, re.M):
                    out.append({"i": int(i), "who": "A" if int(pct) < 50 else "B"})
            log["calls"].append((system[:20], user))
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps({"entries": out})}}], "usage": {}}).encode())
    srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, log
WRONG_SAME_AS = [""]

def run_pass(typing, same_as_b="", wiki_for_morel=True, behaviour=None, citation=lambda s: False, parallel=2):
    WRONG_SAME_AS[0] = same_as_b
    d = Path(tempfile.mkdtemp())
    make_db(d / "x.asc", wiki_for_morel)
    srv, log = make_server(typing, behaviour)
    msgs = []
    opts = SimpleNamespace(base_url=f"http://127.0.0.1:{srv.server_port}/v1", model_high="m", model_bulk="m", parallel=parallel)
    rep = cp.run(d / "x.asc", d / "x.cast", [0], [BOOK], lambda m="": msgs.append(m), opts, is_citation=citation)
    srv.shutdown()
    return rep, d, log, msgs

def q(path, sql, *a):
    c = sqlite3.connect(path); r = c.execute(sql, a).fetchall(); c.close(); return r

TYPING = {"Ursula": ("person", "none"), "Amaranta Ursula": ("person", "none"), "Aureliano": ("person", "none"),
          "Morel": ("person", "mismatch")}
rep, d, log, msgs = run_pass(TYPING)
cast = d / "x.cast"
check("pass writes a repaired COPY and leaves the original untouched",
      cast.exists() and q(d / "x.asc", "SELECT count(*) FROM occurrence")[0][0] == 3 + 2 + 12 + 12 + 54 + 20)
check("typing: terms the model calls people move to the people tab",
      [r[0] for r in q(cast, "SELECT type FROM entity WHERE label = 'Ursula'")] == [1]
      and set(rep["retyped"]) >= {"Ursula", "Morel"} and "Amaranta Ursula" not in rep["retyped"])
check("typing: a Wikipedia text the model calls a mismatch is replaced by the book's own sentence and loses its source",
      q(cast, "SELECT source FROM entity_description d JOIN entity e ON e.id = d.entity WHERE e.label = 'Morel'")[0][0] is None
      and "Morel wrote a letter" in q(cast, "SELECT text FROM entity_description d JOIN entity e ON e.id = d.entity WHERE e.label = 'Morel'")[0][0]
      and rep["wiki_rejected"] == ["Morel"])
n_ursula = len(re.findall(r"(?<![\w])Ursula(?![\w])", BOOK)) - len(MARKS["Amaranta Ursula"])
got = q(cast, "SELECT count(*) FROM occurrence o JOIN entity e ON e.id = o.entity WHERE e.label = 'Ursula'")[0][0]
check("recovery: the untagged mentions of a person come back, none inside 'Amaranta Ursula'", got == n_ursula == 18, f"{got} of {n_ursula}")
check("recovery: no new highlight overlaps another",
      q(cast, "SELECT count(*) FROM occurrence a JOIN occurrence b ON a.start < b.start AND b.start < a.start + a.length")[0][0] == 0)
check("recovery: report counts the mentions added", rep["mentions_added"].get("Ursula") == 15, str(rep["mentions_added"]))
check("shared name: the late stretch becomes its own character, labelled by where in the book it is",
      any(re.fullmatch(r"Aureliano \(\d+-\d+% of the book\)", r[0]) for r in q(cast, "SELECT label FROM entity")) and len(rep["locked"]) == 1)
late_lab = rep["locked"][0]
check("shared name: all 40 late bare mentions moved to the new character, as people",
      q(cast, "SELECT count(*), min(e.type) FROM occurrence o JOIN entity e ON e.id = o.entity WHERE e.label = ?", late_lab)[0] == (40, 1))
check("shared name: 'same as Aureliano Buendia' is accepted where that name's own mentions are (early stretch)",
      q(cast, "SELECT count(*) FROM occurrence o JOIN entity e ON e.id = o.entity WHERE e.label = 'Aureliano Buendia'")[0][0] == 12 + 14)
check("shared name: the emptied bare entry is gone and no occurrence is orphaned",
      q(cast, "SELECT count(*) FROM entity WHERE label = 'Aureliano'")[0][0] == 0
      and q(cast, "SELECT count(*) FROM occurrence WHERE entity NOT IN (SELECT id FROM entity)")[0][0] == 0)
check("counts match the occurrences for every entity",
      q(cast, "SELECT count(*) FROM entity e WHERE count != (SELECT count(*) FROM occurrence o WHERE o.entity = e.id)")[0][0] == 0)
check("new character gets a description row from the book (a sentence), no source",
      q(cast, "SELECT d.source, d.text FROM entity_description d JOIN entity e ON e.id = d.entity WHERE e.label = ?", late_lab)[0][0] is None
      and "parchments" in q(cast, "SELECT d.text FROM entity_description d JOIN entity e ON e.id = d.entity WHERE e.label = ?", late_lab)[0][0])
check("the pass stays within a handful of model calls", log["kinds"].count("type") == 1 and log["kinds"].count("discover") == 1 and log["kinds"].count("assign") <= 6, str(log["kinds"]))

# a wrong "same as": the late scholar is claimed to be Aureliano Segundo, whose own mentions are in the middle
rep2, d2, log2, msgs2 = run_pass(TYPING, same_as_b="Aureliano Segundo")
cast2 = d2 / "x.cast"
check("shared name: a wrong 'same as' (the model's word) is refused when the known character's own mentions are elsewhere",
      q(cast2, "SELECT count(*) FROM occurrence o JOIN entity e ON e.id = o.entity WHERE e.label = 'Aureliano Segundo'")[0][0] == 12
      and any(r[0].startswith("Aureliano (") for r in q(cast2, "SELECT label FROM entity")))

# the model says the Wikipedia text matches: kept
rep3, d3, log3, _ = run_pass({**TYPING, "Morel": ("person", "match")})
check("wiki 'match' keeps the Wikipedia text; typing still applies",
      q(d3 / "x.cast", "SELECT d.source FROM entity_description d JOIN entity e ON e.id = d.entity WHERE e.label = 'Morel'")[0][0] == 1
      and q(d3 / "x.cast", "SELECT type FROM entity WHERE label = 'Morel'")[0][0] == 1)
rep3b, d3b, _, _ = run_pass({**TYPING, "Morel": ("person", "unsure")})
check("wiki 'unsure' keeps the Wikipedia text (only an explicit mismatch removes one)",
      q(d3b / "x.cast", "SELECT d.source FROM entity_description d JOIN entity e ON e.id = d.entity WHERE e.label = 'Morel'")[0][0] == 1)

# the model calls them places/groups: nothing is retyped
rep4, d4, _, msgs4 = run_pass({"Ursula": ("place", "none"), "Aureliano": ("group", "none"), "Morel": ("other", "unsure")})
check("a name the model does not call a person stays a term", rep4 is None or "Ursula" not in rep4["retyped"])
check("... and nothing but deterministic work is done: no shared-name split without people",
      rep4 is None or not rep4["split"])

# fail open: the server errors out on everything
rep5, d5, log5, msgs5 = run_pass(TYPING, behaviour=lambda s, u: "boom")
check("fail open: every request erroring -> no crash, nothing claimed", rep5 is None or (not rep5["retyped"] and not rep5["split"]))
check("fail open: the copy (if any) is still a valid X-Ray",
      not (d5 / "x.cast").exists() or q(d5 / "x.cast", "SELECT count(*) FROM occurrence WHERE entity NOT IN (SELECT id FROM entity)")[0][0] == 0)

# citations: mentions in bibliography-like text are not recovered
rep6, d6, _, _ = run_pass(TYPING, citation=lambda s: "wept again" in s)
check("recovery skips mentions the citation filter rejects", q(d6 / "x.cast", "SELECT count(*) FROM occurrence o JOIN entity e ON e.id = o.entity WHERE e.label = 'Ursula'")[0][0] == 18 - 3)

# -------------------------------------------------------------- integration with enrich
enrich.LOCKED_LABELS.clear(); enrich.LOCKED_LABELS.add("Aureliano (81-99% of the book)")
work = [{"id": 1, "name": "Aureliano (81-99% of the book)"}, {"id": 2, "name": "Other"}]
res = {"Aureliano (81-99% of the book)": SimpleNamespace(grounded=True, canonical_name="Aureliano Buendia"),
       "Other": SimpleNamespace(grounded=True, canonical_name="Someone")}
check("enrich.canonical_names: a label the cast pass created is never renamed by the model, but its card is still written",
      enrich.canonical_names(work, res) == {1: "Aureliano (81-99% of the book)", 2: "Someone"})
enrich.LOCKED_LABELS.clear()

# apply(source=): entities are read from the repaired copy, the real file is replaced, backups come from the real file
dd = Path(tempfile.mkdtemp())
make_db(dd / "real.asc"); make_db(dd / "real.asc.cast")
c = sqlite3.connect(dd / "real.asc.cast"); c.execute("UPDATE entity SET label = 'Ursula Z' WHERE id = 1"); c.commit(); c.close()
tmp_real = dd / "x.asc"; (dd / "real.asc").rename(tmp_real)
srcfile = dd / "x.asc.cast"; (dd / "real.asc.cast").rename(srcfile)
pos_, texts_ = [0], [BOOK]
work_, ents_, occ_ = enrich.collect(srcfile, pos_, texts_)
enrich.apply(tmp_real, work_, ents_, occ_, {}, lambda m="": None, allow_merge=False, source=srcfile)
check("apply(source=...): the replaced file carries the repaired copy's changes",
      q(tmp_real, "SELECT count(*) FROM entity WHERE label = 'Ursula Z'")[0][0] == 1)
check("apply(source=...): .orig is the real file as WordDumb made it, never the repaired copy",
      q(dd / "x.asc.orig", "SELECT count(*) FROM entity WHERE label = 'Ursula Z'")[0][0] == 0 and (dd / "x.asc.bak").exists())


# a locked (cast-pass) entry keeps its label but gets its card written, with page-aware stages too
dl = Path(tempfile.mkdtemp())
make_db(dl / "x.asc")
c = sqlite3.connect(dl / "x.asc"); c.execute("UPDATE entity SET label = 'Aureliano (81-99% of the book)' WHERE id = 5"); c.commit(); c.close()
enrich.LOCKED_LABELS.clear(); enrich.LOCKED_LABELS.add("Aureliano (81-99% of the book)")
wk_l, ents_l, occ_l = enrich.collect(dl / "x.asc", [0], [BOOK])
RES = {"Aureliano (81-99% of the book)": SimpleNamespace(original_name="Aureliano (81-99% of the book)", canonical_name="Aureliano Buendia",
                                                         description="He deciphers the parchments.", grounded=True, stages=[])}
enrich.apply(dl / "x.asc", wk_l, ents_l, occ_l, RES, lambda m="": None, allow_merge=False)
check("apply: a locked entry is not renamed and still receives its card",
      q(dl / "x.asc", "SELECT d.text FROM entity e JOIN entity_description d ON d.entity = e.id WHERE e.label = 'Aureliano (81-99% of the book)'")[0][0] == "He deciphers the parchments."
      and q(dl / "x.asc", "SELECT count(*) FROM entity WHERE label = 'Aureliano Buendia' AND id = 5")[0][0] == 0)
enrich.LOCKED_LABELS.clear()

# a rename that only makes a name longer keeps its label and gets its card; a shorter one stays blocked
dx = Path(tempfile.mkdtemp())
make_db(dx / "x.asc")
c = sqlite3.connect(dx / "x.asc")
c.execute("DELETE FROM entity"); c.execute("DELETE FROM entity_description"); c.execute("DELETE FROM occurrence")
late = MARKS["Aureliano:late"] if "Aureliano:late" in MARKS else [m.start() for m in re.finditer(r"Aureliano read the parchments", BOOK)]
early = [m.start() for m in re.finditer(r"Aureliano looked up", BOOK)]
def add_ent(i, label, typ, starts, source=None):
    c.execute("INSERT INTO entity VALUES (?, ?, 7, ?, ?, 1)", (i, label, typ, len(starts)))
    c.execute("INSERT INTO entity_description VALUES ('raw excerpt', ?, ?, ?)", (label, source, i))
    c.executemany("INSERT INTO occurrence VALUES (?, ?, ?)", [(i, st, len(label)) for st in starts])
add_ent(1, "Morel", 1, late[:30])
add_ent(2, "E. D. Morel", 1, late[30:33], source=1)
add_ent(3, "Yakov Stalin", 1, early[:12])
add_ent(4, "Stalin", 1, early[12:14] + MARKS["Ursula"][:6])
c.commit(); c.close()
wk_x, ents_x, occ_x = enrich.collect(dx / "x.asc", [0], [BOOK])
RES2 = {"Morel": SimpleNamespace(original_name="Morel", canonical_name="E. D. Morel", description="Morel card.", grounded=True, stages=[]),
        "Yakov Stalin": SimpleNamespace(original_name="Yakov Stalin", canonical_name="Stalin", description="A card about Stalin.", grounded=True, stages=[]),
        "Stalin": SimpleNamespace(original_name="Stalin", canonical_name="Stalin", description="Stalin card.", grounded=True, stages=[])}
msgs_x = []
enrich.apply(dx / "x.asc", wk_x, ents_x, occ_x, RES2, lambda m="": msgs_x.append(m), allow_merge=False)
check("apply: 'Morel' -> 'E. D. Morel' (a longer name that already exists) keeps its label and gets its card",
      q(dx / "x.asc", "SELECT d.text FROM entity e JOIN entity_description d ON d.entity = e.id WHERE e.id = 1")[0][0] == "Morel card."
      and q(dx / "x.asc", "SELECT label FROM entity WHERE id = 1")[0][0] == "Morel"
      and q(dx / "x.asc", "SELECT count(*) FROM entity WHERE label = 'E. D. Morel'")[0][0] == 1
      and any("kept their label and got their card" in m for m in msgs_x))
check("apply: 'Yakov Stalin' -> 'Stalin' (a shorter name: a different man) stays exactly as WordDumb made it",
      q(dx / "x.asc", "SELECT d.text FROM entity e JOIN entity_description d ON d.entity = e.id WHERE e.id = 3")[0][0] == "raw excerpt"
      and q(dx / "x.asc", "SELECT label FROM entity WHERE id = 3")[0][0] == "Yakov Stalin")
check("same_name_longer: expansions yes, shorter or different words no",
      enrich.same_name_longer("Casement", "Roger Casement") and enrich.same_name_longer("King Leopold", "Leopold II")
      and not enrich.same_name_longer("Yakov Stalin", "Stalin") and not enrich.same_name_longer("Stephan Sets Out", "Stephan Bagradian"))

enrich.LOCKED_LABELS.clear(); enrich.LOCKED_LABELS.add("Aureliano (81-99% of the book)")
labs_ = {"Aureliano Segundo": 206, "Aureliano Buendia": 200, "Aureliano (81-99% of the book)": 155, "Aureliano José": 37}
check("resolve_other: a bare name never resolves to a split-off character, and stays unresolved while two real ones compete",
      enrich.resolve_other("Aureliano", labs_) is None and enrich.resolve_other("Aureliano", {**labs_, "Aureliano Buendia": 10}) == "Aureliano Segundo")
enrich.LOCKED_LABELS.clear()

enrich.SHARED_BARE.clear(); enrich.SHARED_BARE.add("aureliano")
check("resolve_other: a tie that names only a shared bare name is left unresolved, a full name still resolves",
      enrich.resolve_other("Aureliano", {"Aureliano Segundo": 206, "Aureliano José": 37}) is None
      and enrich.resolve_other("Colonel Aureliano", {"Aureliano Segundo": 206, "Aureliano José": 37}) is None
      and enrich.resolve_other("Aureliano José", {"Aureliano Segundo": 206, "Aureliano José": 37}) == "Aureliano José")
enrich.SHARED_BARE.clear()

EX = ["Gabriel gave his son Stephan an order. Deaf to Ursula's clamor and the teasing of his brother, Aureliano only turned. The enormous box from their grandfather arrived. He had married Fernanda that year."]
vt = lambda rel, other, quote: enrich.valid_tie(rel, other, quote, "Somebody", EX, [0])[:2]
check("valid_tie: the quote must contain a word of the claimed relation (parent/child words for father, mother, son, daughter)",
      vt("father", "Stephan", "Gabriel gave his son Stephan an order") == (True, "")
      and vt("mother", "Ursula", "Deaf to Ursula's clamor and the teasing of his brother") == (False, "word")
      and vt("son", "Gabriel", "The enormous box from their grandfather arrived") == (False, "word")
      and vt("grandfather", "Gabriel", "The enormous box from their grandfather arrived")[0] is True
      and vt("wife", "Fernanda", "He had married Fernanda that year") == (True, "")
      and vt("brother", "Aureliano", "Deaf to Ursula's clamor and the teasing of his brother, Aureliano only turned") == (True, ""))

enrich.LOCKED_LABELS.clear(); enrich.LOCKED_LABELS.add("Jose Arcadio (58-91% of the book)")
enrich.SHARED_BARE.clear(); enrich.SHARED_BARE.add("aureliano")
def pp(label, rels, count=100):
    return {"label": label, "count": count, "rels": rels, "rp": [("Role.", "Progress.", 0)]}
people_ = {"Ursula": pp("Ursula", [{"rel": "mother", "other": "Jose Arcadio (58-91% of the book)", "other_key": "Jose Arcadio (58-91% of the book)", "pos": 10, "n": 1},
                                   {"rel": "wife", "other": "Jose Arcadio Buendia", "other_key": "Jose Arcadio Buendia", "pos": 5, "n": 1}]),
           "Jose Arcadio Buendia": pp("Jose Arcadio Buendia", []),
           "Jose Arcadio (58-91% of the book)": pp("Jose Arcadio (58-91% of the book)", [{"rel": "son", "other": "Ursula", "other_key": "Ursula", "pos": 10, "n": 1}]),
           "Aureliano": pp("Aureliano", [{"rel": "son-in-law", "other": "Don Apolinar", "other_key": "Don Apolinar", "pos": 10, "n": 1}], 30),
           "Don Apolinar": pp("Don Apolinar", [{"rel": "father-in-law", "other": "Aureliano", "pos": 10}], 40)}
cards_, counts_ = enrich.build_people_cards(people_)
txt = {k: v[1] for k, v in cards_.items()}
check("build_people_cards: a split-off entry and the bare shared-name entry get no ties, and nobody is tied to them; the real tie stays",
      "mother of" not in txt["Ursula"].lower() and txt["Ursula"].lower().startswith("wife of jose arcadio buendia")
      and not txt["Jose Arcadio (58-91% of the book)"].lower().startswith(("son of", "husband of"))
      and not txt["Aureliano"].lower().startswith(("son-in-law", "husband of"))
      and "father-in-law" not in txt["Don Apolinar"].lower(), str(txt))
enrich.LOCKED_LABELS.clear(); enrich.SHARED_BARE.clear()

# family ties are shown on every page; a relation word that contradicts the owner's gender is flipped
rp2 = [("Officer", "He leads. He orders.", 0), ("Officer", "He leads. He orders. He defends.", 300)]
ppl = {"Gabriel": {"label": "Gabriel", "count": 100, "rels": [{"rel": "wife", "other": "Juliette", "other_key": "Juliette", "pos": 250}], "rp": rp2},
       "Juliette": {"label": "Juliette", "count": 100, "rels": [{"rel": "wife", "other": "Gabriel", "other_key": "Gabriel", "pos": 250}],
                    "rp": [("Mother", "She walks. She worries.", 0), ("Mother", "She walks. She worries. She weeps.", 300)]},
       "Meme": {"label": "Meme", "count": 100, "rels": [{"rel": "grandson", "other": "Gabriel", "other_key": "Gabriel", "pos": 250}],
                "rp": [("Girl", "She laughs. Her hair.", 0)]}}
pp3, cn3 = enrich.build_people_cards(ppl, gate=True)
check("ties from the first page: a backed family tie stated late is on every page (default)",
      all(c.startswith("Husband of Juliette") for c, _f in pp3["Gabriel"][0]) and all(c.startswith("Wife of Gabriel") for c, _f in pp3["Juliette"][0]), str(pp3["Gabriel"][0]))
check("a relation word that contradicts the owner's own pronouns is flipped ('Wife of Juliette' on Gabriel, 'grandson' on Meme)",
      pp3["Gabriel"][1].startswith("Husband of Juliette") and pp3["Meme"][1].startswith("Granddaughter of Gabriel") and cn3.get("flipped", 0) >= 2, str(pp3["Meme"][1]))
enrich.TIES_FROM_START = False
pp4, _ = enrich.build_people_cards(ppl, gate=True)
check("--ties-from-start no keeps the 1.2.11 gating (no tie before the sentence that states it)", not pp4["Gabriel"][0][0][0].startswith(("Husband", "Wife")), str(pp4["Gabriel"][0]))
enrich.TIES_FROM_START = True

ppa = {"Stephan": {"label": "Stephan", "count": 100, "rels": [{"rel": "son", "other": "Papa", "other_key": "Papa", "pos": 5}, {"rel": "son", "other": "Gabriel", "other_key": "Gabriel", "pos": 5}], "rp": [("Boy", "He runs. He hides.", 0)]},
       "Gabriel": {"label": "Gabriel", "count": 100, "rels": [], "rp": [("Man", "He leads. He orders.", 0)]},
       "Papa": {"label": "Papa", "count": 21, "rels": [], "rp": [("", "Is addressed.", 0)]}}
ppa_cards, _ = enrich.build_people_cards(ppa)
check("a way of addressing someone ('Papa', 21 mentions) is never the other person of a tie", "Papa" not in ppa_cards["Stephan"][1] and ppa_cards["Stephan"][1].startswith("Son of Gabriel."), ppa_cards["Stephan"][1])
check("proper_name: 'Papa' and 'Mother' are not names, 'Papa Joe' is", not cp.proper_name("Papa") and not cp.proper_name("Mother") and cp.proper_name("Papa Joe"))

# ---- kin words in six languages, the title rule, the wider net
import kin_words as kw
tr = kw.triggers
check("kin words: one trigger per language (fr, es, de, ru, ja, en) and the groups they belong to",
      tr("Maman est partie.") == {"PC"} and tr("Mijo, ven acá.") == {"PC"} and tr("Mein Bruder kam heim.") == {"SB"}
      and tr("Это был его отец.") == {"PC"} and tr("お父さんが帰ってきた。") == {"PC"} and tr("Gabriel's wife wept.") == {"SP"}
      and tr("Su hermana Úrsula lloró.") == {"SB"} and tr("Die Großmutter schlief.") == {"GP"}, str([tr("Maman est partie."), tr("Mein Bruder kam heim.")]))
check("kin words: accents and spelling variants fold (mamá, Großvater, otōsan, Onii-san)",
      tr("¡Mamá!") == {"PC"} and tr("Der Großvater kam.") == {"GP"} and tr("Otōsan said so.") == {"PC"} and tr("Onii-san waited.") == {"SB"})
check("title rule: 'Father Nicanor', 'Padre Antonio', 'Sister Agnes' are not ties; 'his father Gabriel', \"Gabriel's son Stephan\", 'Mother, come' are",
      tr("Father Nicanor blessed the house.") == set() and tr("Padre Antonio habló.") == set() and tr("Sister Agnes prayed.") == set()
      and tr("He saw his father Gabriel.") == {"PC"} and tr("Gabriel's son Stephan ran.") == {"PC"} and tr("Mother, come here.") == {"PC"}
      and tr("The mother Ursula wept.") == {"PC"})
check("a family word of the wrong kind does not back a tie (hermano is not a parent word); a word of the right kind in another language does",
      enrich._foreign_kin("brother", "Su hermano llegó.") and not enrich._foreign_kin("father", "Su hermano llegó.")
      and enrich._foreign_kin("father", "Mein Vater schlief.") and enrich._foreign_kin("husband", "彼女の夫が来た。"))
vt2 = enrich.valid_tie("father", "Gabriel", "Der Vater Gabriel sagte nichts", "Stephan", ["Der Vater Gabriel sagte nichts und ging."], [0])
check("valid_tie: a German quote with 'Vater' backs 'father'", vt2[0] is True, str(vt2))
import tempfile as _tf
ex = Path(_tf.mkdtemp()) / "kin.txt"; ex.write_text("# book extras\nmairik mother\nhayr father\nbogus nothing\n", encoding="utf-8")
kw.reload(ex)
check("extras file: 'mairik' (mother) and 'hayr' (father) are added, a bad line is ignored", tr("Mairik Antaram wept.") == set() and tr("His mairik wept.") == {"PC"} and tr("Hayr, bless us.") == {"PC"})
kw.reload(Path("/nonexistent"))
W_TXT = "x" * 40 + " Juliette came in. Her husband Gabriel spoke to nobody. " + "y" * 300 + " Father Nicanor spoke with Juliette and Gabriel. " + "z" * 50
wk_ = [{"id": 1, "name": "Juliette", "type": 1, "count": 30}, {"id": 2, "name": "Gabriel", "type": 1, "count": 30}]
occ_ = {1: [(W_TXT.index("Juliette"), 8), (W_TXT.rindex("Juliette"), 8)], 2: [(W_TXT.index("Gabriel"), 7), (W_TXT.rindex("Gabriel"), 7)]}
cc = enrich.find_tie_candidates(wk_, occ_, [0], [W_TXT], min_mentions=1)
check("wider net: the kin sentence plus the two before it name both people (one candidate from the window); a priest's title is no candidate",
      len(cc) == 1 and sorted(cc[0]["names"]) == ["Gabriel", "Juliette"] and "Her husband" in cc[0]["trigger"] and "Juliette came in" in cc[0]["sentence"], str(cc))
cc1 = enrich.find_tie_candidates(wk_, occ_, [0], [W_TXT], min_mentions=1, window=1)
check("... and with a window of one sentence (the 1.2.11 scan) that candidate is not found", len(cc1) == 0, str(cc1))

Wn = "x" * 40 + " Juliette and her husband Gabriel left the house. " + "z" * 50
cn = enrich.find_tie_candidates([{"id": 1, "name": "Juliette", "type": 1, "count": 30}, {"id": 2, "name": "Gabriel", "type": 1, "count": 30}],
                                {1: [(Wn.index("Juliette"), 8)], 2: [(Wn.index("Gabriel"), 7)]}, [0], [Wn], min_mentions=1)
check("wider net: a candidate says whether both people are in the sentence with the family word (narrow True) or only in the window (narrow False)",
      len(cn) == 1 and cn[0]["narrow"] is True and cc[0]["narrow"] is False, str((cn, cc[:1])))
Wd = "x" * 40 + " Juliette came in. Her husband spoke to nobody, then Gabriel left. " + "z" * 50
wk2 = [{"id": 1, "name": "Juliette", "type": 1, "count": 30}, {"id": 2, "name": "Gabriel", "type": 1, "count": 30}]
oc2 = {1: [(Wd.index("Juliette"), 8)], 2: [(Wd.index("Gabriel"), 7)]}
cw = enrich.find_tie_candidates(wk2, oc2, [0], [Wd], min_mentions=1)
check("wider net: 'Juliette came in. Her husband spoke ... Gabriel' is a window-only candidate (narrow False)", len(cw) == 1 and cw[0]["narrow"] is False, str(cw))

# -------------------------------------------------------------- a connection dropped in the middle of a reply
class Flaky(BaseHTTPRequestHandler):
    hits = {"n": 0}
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Flaky.hits["n"] += 1
        if Flaky.hits["n"] == 1:                       # announce a chunked reply, send a fragment, hang up
            self.send_response(200); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
            self.wfile.write(b"10\r\n{\"choices\": [{\"m"); self.wfile.flush(); self.close_connection = True; return
        user = body["messages"][1]["content"]
        names = re.findall(r"^### (.+?)  \(", user, re.M)
        out = {"entries": [{"original_name": n, "canonical_name": n, "description": f"desc of {n}"} for n in names],
               "entries2": []}
        if body["messages"][0]["content"].startswith("For each numbered name"):
            out = {"entries": [{"i": 0, "kind": "person", "wiki": "none"}]}
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps(out)}}], "usage": {}}).encode())
srv = HTTPServer(("127.0.0.1", 0), Flaky); threading.Thread(target=srv.serve_forever, daemon=True).start()
cl = cp.Client(f"http://127.0.0.1:{srv.server_port}/v1", "m")
got = cl.ask(cp.TYPE_SYSTEM, "[0] X", cp.TYPE_SCHEMA)
check("cast pass client: a reply cut off mid-body (IncompleteRead) is retried, not fatal", got == [{"i": 0, "kind": "person", "wiki": "none"}] and Flaky.hits["n"] == 2, str(Flaky.hits))
Flaky.hits["n"] = 0
enrich.BACKOFF = (0.05, 0.05, 0.05)
work_ = [{"id": 1, "name": "Ent1", "type": 1, "count": 3, "excerpts": ["x" * 120]}]
res_ = enrich.enrich_local(work_, lambda m="": None, SimpleNamespace(base_url=f"http://127.0.0.1:{srv.server_port}/v1", model_bulk="m", model_high="",
                           parallel=1, provider="local", people_layout="no"))
check("enrich_local: a reply cut off mid-body is retried instead of crashing the run (it killed a real run before 1.2.12)",
      "Ent1" in res_ and Flaky.hits["n"] == 2, str(Flaky.hits))
srv.shutdown()

print("\nALL PASSED" if not FAILED else f"\n{FAILED} FAILED")
sys.exit(1 if FAILED else 0)
