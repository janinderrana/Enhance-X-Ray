"""1.2.15: pronunciation lines. python3 test_ipa.py [dir containing ipa_pass.py]"""
import json, re, sqlite3, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent))
import ipa_pass as ip

FAILED = 0
def check(label, ok, extra=""):
    global FAILED
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else "")); FAILED += (not ok)

# --- IPA text
check("plain IPA kept", ip.clean_ipa("/ɡɒnˈzɑːɡ/") == "ɡɒnˈzɑːɡ")
check("ascii g becomes ɡ", ip.clean_ipa("gɒn") == "ɡɒn")
check("a quoted spelling is not IPA", ip.clean_ipa('"Stanley"') == "")
check("digits, brackets and commas are not IPA", ip.clean_ipa("ba1") == "" and ip.clean_ipa("ba, bo") == "" and ip.clean_ipa("a(b)") == "")
check("capital letters are not IPA", ip.clean_ipa("Stanley") == "")
check("nasal vowel with a combining tilde passes", ip.clean_ipa("ɡɑ̃ˈzaɡ") != "")
check("empty and too long", ip.clean_ipa("") == "" and ip.clean_ipa("a" * 60) == "")
check("line: both accents", ip.make_line("ɡɒnˈzɑːɡ", "ɡɑːnˈzɑːɡ", True) == "≈ UK /ɡɒnˈzɑːɡ/ · US /ɡɑːnˈzɑːɡ/")
check("line: same in both", ip.make_line("bɑː", "bɑː", False) == "UK & US /bɑː/")
check("line: UK only (dictionary)", ip.make_line("ˈstænlɪ", "", False) == "UK /ˈstænlɪ/")
check("line: US only", ip.make_line("", "ˈstænli", True) == "≈ US /ˈstænli/")
check("line: nothing", ip.make_line("", "", True) == "")
check("a written line is recognised again", all(ip.LINE_RE.match(l + "\nText") for l in
      ("UK /a/", "US /a/", "≈ UK /a/ · US /b/", "UK & US /a/")) and not ip.LINE_RE.match("Uncle of Juan\nx"))

# --- dictionary markup (Collins' own shapes)
HEAD = '<p height="15" width="0" align="left"><span><font size="5" color="#0038A8"><b>%s</b></font></span>%s</p>'
SC = '<span><font size="-1">I</font></span>'
def entry(head, rest): return HEAD % (head, rest)
markup = "".join([
    entry("Stanley", f' (<span>ˈstænl{SC}</span>)   <span><i>n</i></span> a name'),
    entry('Paris<span><font size="-1"><b><sup>1</sup></b></font></span>', f' (<span>ˈpær{SC}s; <i>French</i> pari)</span>   <span><i>n</i></span>'),
    entry("Werfel", ' (<span><i>German</i></span><span> ˈvɛrfəl</span>)   <span><i>n</i></span>'),
    entry("Baghdad", ' or <span><b>Bagdad</b></span> (bæɡˈdæd)   <span><i>n</i></span>'),
    entry("Damascus", ' (<span>dəˈmɑːskəs, -ˈmæs-</span>)   <span><i>n</i></span>'),
    entry("Port Said", f' (<span>ˈsɑːiːd, sa{SC}d</span>)   <span><i>n</i></span>'),
    entry("cart", ' (<span>kɑːt</span>)   <span><i>n</i></span>'),
    entry("Gabriel", f' (<span>ˈɡe{SC}br{SC}əl</span>)   <span><i>n</i></span>'),
])
ix = ip.parse_entries(markup)
check("a headword and its IPA (small-capital I read as ɪ)", ix.get("Stanley") == "ˈstænlɪ", ix.get("Stanley"))
check("superscript homograph number removed; the English reading, not the French", ix.get("Paris") == "ˈpærɪs", ix.get("Paris"))
check("a name read the foreign way only has no English reading", "Werfel" not in ix)
check("an 'or' variant after the headword", ix.get("Baghdad") == "bæɡˈdæd", ix.get("Baghdad"))
check("first of several variants", ix.get("Damascus") == "dəˈmɑːskəs", ix.get("Damascus"))
check("a multi-word headword whose IPA covers only part of it is dropped", "Port Said" not in ix)
check("lower-case words are not names", "cart" not in ix)
check("small capital I inside a diphthong", ix.get("Gabriel") == "ˈɡeɪbrɪəl", ix.get("Gabriel"))

check("numeral split", ip.split_numeral("Leopold II") == ("Leopold", "II") and ip.split_numeral("Louis XIV") == ("Louis XIV", "")
      and ip.split_numeral("Pius X") == ("Pius", "X") and ip.split_numeral("Ivan") == ("Ivan", ""))
check("syllable dots dropped", ip.clean_ipa("ˈhɛn.ri") == "ˈhɛnri")
d = {"Stanley": "ˈstænlɪ", "Joseph": "ˈdʒəʊzɪf", "Stalin": "ˈstɑːlɪn", "Adams": "ˈædəmz"}
check("whole name", ip.dictionary_ipa(d, "Stanley") == "ˈstænlɪ")
check("a title in front is ignored", ip.dictionary_ipa(d, "Dr. Adams") == "ˈædəmz")
check("every word known -> joined", ip.dictionary_ipa(d, "Joseph Stalin") == "ˈdʒəʊzɪf ˈstɑːlɪn")
check("one word unknown -> none (the model gets the whole name)", ip.dictionary_ipa(d, "Joseph Bagradian") == "")

# --- the X-Ray pass, with a stub model
def make_xray(path):
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE entity (id INTEGER PRIMARY KEY, label TEXT, loc_label TEXT, type INTEGER, count INTEGER, has_info_card INTEGER);"
                    "CREATE TABLE entity_description (text TEXT, source_wildcard TEXT, source INTEGER, entity INTEGER);")
    rows = [(1, "Gonzague", 1, 40, "A French-speaking Armenian."), (2, "Gonzague", 1, 10, "Later he leaves."),
            (3, "Stanley", 1, 30, "A teacher's pet."), (4, "Aintab", 2, 20, "A town."), (5, "Stephan", 1, 50, "Gabriel's son."),
            (6, "Aureliano (7-38% of the book)", 1, 23, "Son of the colonel."), (7, "Rare", 1, 1, "Seen once."),
            (9, "mukhtar", 2, 30, "A village headman."),
            (10, "Nunik, Vartuk", 1, 9, "Three women."), (11, "Wikiman", 1, 12, "Real man."),
            (12, "Leopold II", 1, 20, "A king."), (13, "Henry I. Kowalsky", 1, 10, "A colonel.")]
    for i, lab, typ, n, text in rows:
        c.execute("INSERT INTO entity VALUES (?,?,?,?,?,0)", (i, lab, lab, typ, n))
        c.execute("INSERT INTO entity_description VALUES (?,?,?,?)", ("UK /x/\n" + text if i == 3 else text, None, 1 if i == 11 else None, i))
    c.commit(); c.close()

def texts(path):
    return {r[0]: r[1] for r in sqlite3.connect(path).execute("SELECT entity, text FROM entity_description")}

class Stub:
    """what the local model would answer"""
    def ask(self, system, user, schema, retries=2):
        rows = [re.match(r"\[(\d+)\] (.*?)(?: \(|$)", l) for l in user.splitlines()]
        if "help" in schema["properties"]["entries"]["items"]["properties"]:
            return [{"i": int(m.group(1)), "help": m.group(2) != "Stanley"} for m in rows]
        out = []
        for m in rows:
            ans = {"Gonzague": ("French", "ɡɔ̃zaɡ", "ɡɒnˈzɑːɡ", "ɡɑːnˈzɑːɡ"), "Stephan": ("English", "ˈstɛfən", "", ""),
                   "Aureliano": ("Spanish", "", "", ""), "Wikiman": ("French", "", "ˈwɪkiˌmæn", "ˈwɪkiˌmæn"),
                   "Rare": ("English", "", "", ""), "Leopold": ("English", "", "ˈliːəpəʊld", "ˈliːəpoʊld"), "Aintab": ("Turkish", "", "aɪnˈtæp", "aɪnˈtæp")}.get(m.group(2), ("English", "", "", ""))
            out.append({"i": int(m.group(1)), "language": ans[0], "native": ans[1], "uk": ans[2], "us": ans[3]})
        return out

DICT = {"Stanley": "ˈstænlɪ", "Aintab": "aɪnˈtɑːb"}
with tempfile.TemporaryDirectory() as td:
    x = Path(td) / "x.asc"; make_xray(x)
    msgs = []
    st = ip.annotate(x, msgs.append, index=DICT, client=Stub(), workers=1)
    text = texts(x)
    first = lambda i: text[i].split("\n")[0]
    check("a model guess is marked ≈ with both accents, then a line break, then the entry",
          text[1] == "≈ UK /ɡɒnˈzɑːɡ/ · US /ɡɑːnˈzɑːɡ/\nA French-speaking Armenian.", repr(text[1]))
    check("every page card of a character gets the line", first(2).startswith("≈ UK /ɡɒnˈzɑːɡ/") and text[2].endswith("Later he leaves."))
    check("an ordinary name the dictionary knows gets none, and the stale line it had is not kept", "ˈstænlɪ" not in text[3])
    check("a dictionary name that needs help: no ≈, UK only, and the dictionary wins over the model's reading",
          text[4] == "UK /aɪnˈtɑːb/\nA town.", repr(text[4]))
    check("an ordinary English name the model leaves empty gets none", text[5] == "Gabriel's son.")
    check("a name the model gives no reading for gets none", text[6] == "Son of the colonel.")
    check("a name mentioned fewer than 3 times is not considered", text[7] == "Seen once.")
    check("a lower-case term is not a name", text[9] == "A village headman.")
    check("a list of names is not one name", text[10] == "Three women.")
    check("a Wikipedia-described entry gets the line too, its source untouched",
          first(11).startswith("≈ UK") and sqlite3.connect(x).execute("SELECT source FROM entity_description WHERE entity = 11").fetchone()[0] == 1)
    check("a Roman numeral is spoken as an ordinal after the name's reading",
          first(12) == "≈ UK /ˈliːəpəʊld ðə ˈsɛkənd/ · US /ˈliːəpoʊld ðə ˈsɛkənd/", first(12))
    check("a name with an initial is left alone", text[13] == "A colonel.")
    check("stats", st["lines"] == 4 and st["rows"] == 5 and st["dictionary"] == 1 and st["model"] == 3, str(st))
    ip.annotate(x, msgs.append, index=DICT, client=Stub(), workers=1)
    check("a re-run replaces the lines and never stacks them", texts(x) == text)
    sqlite3.connect(x).execute("SELECT 1")
    c2 = sqlite3.connect(x); c2.execute("UPDATE entity SET count = 1 WHERE id = 1 OR id = 2"); c2.commit(); c2.close()
    ip.annotate(x, msgs.append, index=DICT, client=Stub(), workers=1)
    check("a name that no longer qualifies loses its old line", texts(x)[1] == "A French-speaking Armenian.")

    y = Path(td) / "y.asc"; make_xray(y)
    ip.annotate(y, msgs.append, index=DICT, client=None)
    t3 = texts(y)
    check("without a model nothing is guessed; dictionary names are shown", t3[1] == "A French-speaking Armenian." and t3[4].startswith("UK /aɪnˈtɑːb/"))

    z = Path(td) / "z.asc"; make_xray(z)
    ip.annotate(z, msgs.append, index={"Stephan": "ˈstiːvən"}, client=None, people_from_dictionary=False)
    check("a novel's character is not read from the dictionary", texts(z)[5] == "Gabriel's son.")
    z2 = Path(td) / "z2.asc"; make_xray(z2)
    ip.annotate(z2, msgs.append, index={"Stephan": "ˈstiːvən"}, client=None, people_from_dictionary=True)
    check("a non-fiction person is", texts(z2)[5].startswith("UK /ˈstiːvən/\n"))

    class Dead:
        calls = 0
        def ask(self, *a, **k): return None
    w = Path(td) / "w.asc"; make_xray(w)
    st = ip.annotate(w, msgs.append, index={"Aintab": "aɪnˈtɑːb"}, client=Dead())
    check("a dead model: no lines, no crash", st["lines"] == 0, str(st))

    f = Path(td) / "i.json"; f.write_text(json.dumps({"version": 1, "names": {"A": "b"}}), encoding="utf-8")
    check("index load", ip.load_index(f) == {"A": "b"} and ip.load_index(Path(td) / "none.json") == {})
print("\nFAILED:", FAILED)
sys.exit(1 if FAILED else 0)
