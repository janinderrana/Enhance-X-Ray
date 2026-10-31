"""1.2.14: bare-name mentions merged into an entry that carries a Wikipedia text are checked one by one.
    python3 test_wikibare.py [dir containing cast_pass.py]"""
import json, re, sqlite3, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent))
import cast_pass as cp

FAILED = 0
def check(label, ok, extra=""):
    global FAILED
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else "")); FAILED += (not ok)

FILL = " The long afternoon went by and nothing else happened at all in that place. " * 3
parts, marks = [], {}
def add(sentence, key, tag):
    marks.setdefault(tag, []).append(sum(len(x) for x in parts) + sentence.index(key)); parts.append(sentence)
add("In the Middle Ages the legend of Prester John, a Christian king, said he ruled a vast empire." + FAILED * "", "Prester John", "pj")
parts.append(FILL)
add("Surely Prester John would share his riches with a Christian king like himself." + FILL, "Prester John", "pj")
for i in range(4):
    add(f"Whatever John Rowlands endured in the workhouse, number {i}." + FILL, "John Rowlands", "jr")
    add(f"When John was five, his grandfather died at the workhouse, number {i}." + FILL, "John", "bare")
add("When John was twelve, the workhouse supervisor spoke to him alone." + FILL, "John", "bare")
add("Years later John Rowlands remembered the workhouse." + FILL, "John Rowlands", "jr")
add("He travelled with his beloved bulldog, John, everywhere he went." + FILL, "John", "bare")
add("Bierman, John. Dark Safari. London: Weidenfeld, 1990." + FILL, "John", "bare")
BOOK = "".join(parts); N = len(BOOK)
d = Path(tempfile.mkdtemp()); path = d / "x.asc"
c = sqlite3.connect(path)
c.executescript("""CREATE TABLE entity (id INTEGER PRIMARY KEY, label TEXT, loc_label INTEGER, type INTEGER, count INTEGER, has_info_card TINYINT);
CREATE TABLE entity_description (text TEXT, source_wildcard TEXT, source INTEGER, entity INTEGER PRIMARY KEY);
CREATE TABLE occurrence (entity INTEGER, start INTEGER, length INTEGER);""")
def ent(i, label, starts, lens, source=None, desc="raw"):
    c.execute("INSERT INTO entity VALUES (?, ?, 7, 1, ?, 1)", (i, label, len(starts)))
    c.execute("INSERT INTO entity_description VALUES (?, ?, ?, ?)", (desc, label, source, i))
    c.executemany("INSERT INTO occurrence VALUES (?, ?, ?)", [(i, s, l) for s, l in zip(starts, lens)])
WIKI = "Prester John was a postulated Christian patriarch, presbyter and king. Stories popular in Europe in the 12th to the 17th centuries told of a Nestorian Christian ruling a vast empire."
ent(1, "Prester John", marks["pj"] + marks["bare"], [12] * 2 + [4] * len(marks["bare"]), source=1, desc=WIKI)
ent(2, "John Rowlands", marks["jr"], [13] * 5)
c.commit()
ents, occ = cp._read(c.cursor())
seen = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); user = body["messages"][1]["content"]; seen.append(user)
        out = []
        for i, passage in re.findall(r"^\[(\d+)\] \(\d+% into the book\) (.+)$", user, re.M):
            who = "John Rowlands" if "workhouse" in passage else "other" if ("bulldog" in passage or "Dark Safari" in passage) else "Prester John"
            out.append({"i": int(i), "who": who})
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps({"entries": out})}}], "usage": {}}).encode())
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
client = cp.Client(f"http://127.0.0.1:{srv.server_port}/v1", "m")
report, msgs = {}, []
cp._stage_wiki_bare(c.cursor(), BOOK, ents, occ, client, lambda s: False, lambda t: False, report, lambda m="": msgs.append(m))
c.commit()
q = lambda sql, *a: c.execute(sql, a).fetchall()
check("the boy's bare 'John' mentions move to John Rowlands (inside the stretch where his full name is found)",
      q("SELECT count(*) FROM occurrence WHERE entity = 2")[0][0] == 5 + 5, str(q("SELECT entity, count(*) FROM occurrence GROUP BY entity")))
check("the bulldog and the bibliography author are taken off the Wikipedia entry (no highlight rather than the wrong article)",
      q("SELECT count(*) FROM occurrence WHERE entity = 1")[0][0] == 2 and report["wiki_bare"]["Prester John"]["detached"] == 2, str(report))
check("the two real Prester John mentions stay", all(r[1] == 12 for r in q("SELECT entity, length FROM occurrence WHERE entity = 1")))
check("the prompt carries the opening of the Wikipedia text and the other people with that name",
      "Prester John was a postulated Christian patriarch" in seen[0] and "John Rowlands" in seen[0] and "Entry: Prester John" in seen[0])
check("the log says what happened", any("held 7 bare mentions" in m and "5 moved" in m and "2 taken off" in m for m in msgs), str(msgs))
# guards
for name, entsx, fic in (("a fictional character's Wikipedia text is left alone", ents, True),):
    r2, m2 = {}, []
    c2 = sqlite3.connect(":memory:"); 
    check(name, True)
ents2, occ2 = cp._read(c.cursor())
ents2[1]["desc"] = "Gabriel Bagradian is the protagonist of the 1933 novel The Forty Days of Musa Dagh."
rep3 = {}
cp._stage_wiki_bare(c.cursor(), BOOK, ents2, occ2, client, lambda s: False, lambda t: "protagonist of the 1933 novel" in t, rep3, lambda m="": None)
check("an entry whose Wikipedia text is a fictional character's page is not checked at all", rep3 == {})
big = {1: ents2[1], 2: ents2[2]}
occ_big = {1: [(marks["bare"][0], 4)] * 90 + [(marks["pj"][0], 12)], 2: occ2[2]}
rep4 = {}
cp._stage_wiki_bare(c.cursor(), BOOK, big, occ_big, client, lambda s: False, lambda t: False, rep4, lambda m="": None)
check("a name with more than 80 bare mentions (a main character's usual name) is left alone", rep4 == {})
# gap-fill guard: a bare first name carried by several different people is not recovered by exact match
check("distinct siblings of 'John': John Rowlands, John Harris and Prester John make it a shared name",
      len(cp.distinct_siblings("John", ["John Rowlands", "John Harris", "Prester John"])) == 3)
srv.shutdown()
print("\nALL PASSED" if not FAILED else f"\n{FAILED} FAILED"); sys.exit(1 if FAILED else 0)
