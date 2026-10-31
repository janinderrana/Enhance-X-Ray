"""Pronunciation lines (v1.2.15): "UK /ɡɒnˈzɑːɡ/ · US /ɡɑːnˈzɑːɡ/" in front of an X-Ray entry, then a line break, then the entry.

Where a pronunciation comes from, in order:
  1. a dictionary in the user's calibre library (Collins English Dictionary; its proper-name entries give a British IPA for
     real people and places). The headwords are read once into a small JSON index; a lookup costs nothing.
  2. the local model, for the names the dictionary does not have (the book's own characters and villages): it first says
     which language the name comes from and how a native speaker says it, then how an English speaker says it in British
     and in American English. Those lines are marked "≈": they are a careful guess, not a dictionary entry.
Ordinary English names (John, London, Stanley) get no line. A name gets one accent when only one is known, and the
accent is always named, so "UK" never passes for "US".

Runs after the cards are written, over the finished X-Ray file, so it covers every entry (Wikipedia-described ones too)
and every page card of a character. Nothing here may fail the job.
"""
import html
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LINE_RE = re.compile(r"\A(?:≈ )?(?:UK|US)\b[^\n]*\n")      # a line this module wrote (a re-run replaces it)
BOOK_SUFFIX = re.compile(r"\s*\(\d+\s*[-–]\s*\d+% of the book\)\s*$")     # the cast pass's "Aureliano (7-38% of the book)"
INDEX_VERSION = 1
MAX_CANDIDATES = 150
MIN_MENTIONS = 3
BATCH = 25
TITLES = set("""mr mrs ms miss dr doctor sir lady lord king queen prince princess captain colonel general major sergeant
    pastor father brother sister saint st mother madame monsieur mme mlle herr frau don dona count baron""".split())

# ------------------------------------------------------------------------------------------------ IPA text

_NON_IPA = set("\"'`/[]()<>{},;:!?0123456789")


def clean_ipa(s) -> str:
    """The IPA in a model's or a dictionary's answer, or "" when it is not plain IPA (a quoted spelling, digits, brackets)"""
    s = (s or "").strip().strip("/[] ").replace("g", "ɡ").replace(".", "")      # syllable dots are noise
    if not s or len(s) > 45 or any(c in _NON_IPA for c in s):
        return ""
    for c in s:
        if c in " -‿|ˈˌːˑ":
            continue
        o = ord(c)
        if "a" <= c <= "z" or 0x00E0 <= o <= 0x024F or 0x0250 <= o <= 0x02FF or 0x0300 <= o <= 0x036F or 0x1D00 <= o <= 0x1DBF:
            continue
        return ""
    return s


def make_line(uk: str, us: str, guess: bool) -> str:
    uk, us = clean_ipa(uk), clean_ipa(us)
    if uk and us:
        body = f"UK & US /{uk}/" if uk == us else f"UK /{uk}/ · US /{us}/"
    elif uk:
        body = f"UK /{uk}/"
    elif us:
        body = f"US /{us}/"
    else:
        return ""
    return ("≈ " if guess else "") + body


# a trailing Roman numeral is spoken as an ordinal: "Leopold II" -> "Leopold the second"
ORDINALS = {"I": ("ðə ˈfɜːst", "ðə ˈfɝst"), "II": ("ðə ˈsɛkənd", "ðə ˈsɛkənd"), "III": ("ðə ˈθɜːd", "ðə ˈθɝd"),
            "IV": ("ðə ˈfɔːθ", "ðə ˈfɔrθ"), "V": ("ðə ˈfɪfθ", "ðə ˈfɪfθ"), "VI": ("ðə ˈsɪksθ", "ðə ˈsɪksθ"),
            "VII": ("ðə ˈsɛvənθ", "ðə ˈsɛvənθ"), "VIII": ("ðə eɪtθ", "ðə eɪtθ"), "IX": ("ðə naɪnθ", "ðə naɪnθ"),
            "X": ("ðə tɛnθ", "ðə tɛnθ")}
_NUMERAL = re.compile(r"\A(.*\S)\s+(" + "|".join(sorted(ORDINALS, key=len, reverse=True)) + r")\Z")


def split_numeral(name: str):
    """"Leopold II" -> ("Leopold", "II"); a name without one -> (name, "")"""
    m = _NUMERAL.match(name)
    return (m.group(1), m.group(2)) if m else (name, "")


# -------------------------------------------------------------------------------------- dictionary index

_ENTRY = re.compile(r'<p height="15" width="0" align="left"><span><font size="5" color="#0038A8"><b>(.*?)</b></font></span>'
                    r'(.*?)</p>', re.S)


def _plain(s: str) -> str:
    s = s.replace('<font size="-1">I</font>', "ɪ")          # Collins writes ɪ as a small capital I
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def _english_ipa(group: str) -> str:
    """"ˈpærɪs; French pari" -> ˈpærɪs. "French ɡabriɛl" (a name read the foreign way only) -> "" """
    first = group.split(";")[0].strip()
    if re.match(r"[A-Z][a-z]+\s", first):
        return ""
    return clean_ipa(first.split(",")[0].strip().strip("-"))


def parse_entries(markup: str) -> dict:
    """headword (capitalised only: names and places) -> its first English IPA"""
    out = {}
    for m in _ENTRY.finditer(markup):
        head = _plain(re.sub(r"<sup>.*?</sup>", "", m.group(1), flags=re.S)).strip()
        if not head or not head[0].isupper() or head in out:
            continue
        rest = _plain(m.group(2)[:600])
        g = re.match(r"\s*(?:or\s+[^()]{1,40}?\s+)?\(([^()]+)\)", rest)
        ipa = _english_ipa(g.group(1)) if g else ""
        if ipa and len(ipa.split()) != len(head.split()):      # "Port Said (ˈsɑːiːd)": the dictionary spells out the new part only
            continue
        if ipa:
            out[head] = ipa
    return out


def build_index(book: Path, out: Path) -> int:
    """Read the dictionary book (MOBI) with calibre's own reader and write the headword index to `out`. Needs calibre."""
    from calibre.ebooks.mobi.reader.mobi6 import MobiReader
    from calibre.utils.logging import Log
    reader = MobiReader(str(book), Log())
    reader.extract_text()
    markup = reader.mobi_html
    markup = markup.decode("utf-8", "replace") if isinstance(markup, bytes) else markup
    names = parse_entries(markup)
    st = Path(book).stat()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": INDEX_VERSION, "source": str(book), "size": st.st_size, "mtime": int(st.st_mtime),
                               "names": names}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return len(names)


def index_is_current(book: Path, index: Path) -> bool:
    try:
        d = json.loads(index.read_text(encoding="utf-8"))
        st = Path(book).stat()
        return d.get("version") == INDEX_VERSION and d.get("source") == str(book) and d.get("size") == st.st_size \
            and d.get("mtime") == int(st.st_mtime)
    except (OSError, ValueError):
        return False


def load_index(path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d.get("names", {}) if d.get("version") == INDEX_VERSION else {}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def dictionary_ipa(index: dict, base: str) -> str:
    """The dictionary's IPA for a whole name, or for each of its words ("Joseph Stalin"); "" when any part is missing"""
    if base in index:
        return index[base]
    toks = [t for t in re.findall(r"[^\s]+", base) if t.lower().strip(".") not in TITLES]
    core = " ".join(toks)
    if core and core != base and core in index:
        return index[core]
    if len(toks) > 1 and all(t[0].isupper() and t in index for t in toks):
        return " ".join(index[t] for t in toks)
    return ""


# ----------------------------------------------------------------------------------------------- the model

HELP_SYSTEM = ("A reader of a book is shown a list of names of people and places. For each numbered name say whether the "
               "spelling leaves an English-speaking reader unsure how to say it: help=true for foreign names, unusual "
               "spellings and names English speakers say differently from how they read them; help=false for ordinary "
               "English names and places that an English speaker says correctly on sight (John, Mary, Henry, London, "
               "Stanley, Washington, Paris).")
HELP_SCHEMA = None            # built lazily (needs cast_pass)

IPA_SYSTEM = ("You write pronunciations of names from a book. For each numbered name give: language (the language the name "
              "comes from: French, Armenian, Turkish, German, English ...), native (the IPA a native speaker would use, no "
              "slashes), uk (British English RP, non-rhotic: no r before a consonant or at the end) and us (General American, rhotic): how an educated English speaker says it, "
              "keeping the sounds English speakers normally keep and adapting the rest. IPA with stress marks, no "
              "slashes, never the spelling. Say the name as it is written, never translate it. Give \"\" for uk and us when the name is an ordinary English word or an "
              "English name whose pronunciation is obvious (John, Mary, London). Follow the spelling rules of the "
              "language: French 'gue' is a hard g and final consonants are often silent, Armenian 'gh' is a voiced velar, "
              "Turkish 'ğ' lengthens the vowel before it, German 'w' is v.")


def _schemas():
    import cast_pass
    return (cast_pass._schema({"i": {"type": "integer"}, "help": {"type": "boolean"}}),
            cast_pass._schema({"i": {"type": "integer"}, "language": {"type": "string"}, "native": {"type": "string"},
                               "uk": {"type": "string"}, "us": {"type": "string"}}))


def _hint(desc: str, kind: int) -> str:
    first = re.split(r"(?<=[.!?])\s", (LINE_RE.sub("", desc or "")).strip(), maxsplit=1)[0][:90]
    return ("person" if kind == 1 else "place or thing") + (f"; {first}" if first else "")


def _chunks(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _ask_all(client, system, schema, groups, workers, render):
    """groups: list of lists of names -> list of {i: answer} (None for a failed request), asked in parallel"""
    def one(group):
        user = "\n".join(render(i, g) for i, g in enumerate(group))
        res = client.ask(system, user, schema)
        return {r.get("i"): r for r in (res or []) if isinstance(r, dict)} if res is not None else None
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, groups))


def needs_help(client, names, workers=4):
    """Of the names the dictionary knows, the ones a reader would still stumble over -> set of names. A name the model
    does not answer for gets no line: a missing line costs less than a pronunciation line under "John"."""
    help_schema, _ = _schemas()
    groups = _chunks(names, 60)
    answers = _ask_all(client, HELP_SYSTEM, help_schema, groups, workers, lambda i, name: f"[{i}] {name}")
    return {name for group, ans in zip(groups, answers) for i, name in enumerate(group)
            if (ans or {}).get(i, {}).get("help") is True}


def _echoes_spelling(ipa: str, name: str) -> bool:
    """plain letters, no stress mark, the name's own letters: the model handed the spelling back as "IPA" (Stanley -> stanley)"""
    return ipa.isascii() and "ˈ" not in ipa and ipa.replace(" ", "") == re.sub(r"\W", "", name.lower())


def model_ipa(client, items, workers=4):
    """items: [(name, hint)] -> {name: (uk, us)} for the names the model pronounced (ordinary English names are left out)"""
    _, ipa_schema = _schemas()
    groups = _chunks(items, BATCH)
    answers = _ask_all(client, IPA_SYSTEM, ipa_schema, groups, workers, lambda i, item: f"[{i}] {item[0]} ({item[1]})")
    out = {}
    for group, ans in zip(groups, answers):
        for i, (name, _hint_) in enumerate(group):
            r = (ans or {}).get(i)
            if not r:
                continue
            uk, us = clean_ipa(r.get("uk")), clean_ipa(r.get("us"))
            uk, us = ("" if _echoes_spelling(uk, name) else uk), ("" if _echoes_spelling(us, name) else us)
            # one pronounced word for each written word: anything else is the model losing the thread
            n_words = len(name.split())
            uk, us = ("" if len(uk.split()) != n_words else uk), ("" if len(us.split()) != n_words else us)
            if uk or us:
                out[name] = (uk, us)
    return out


# -------------------------------------------------------------------------------------------- the X-Ray

def candidates(cur, limit=MAX_CANDIDATES):
    """[(name, kind, hint source id list, mentions)] -- capitalised labels (people, places), most mentioned first"""
    byname = {}
    for eid, label, typ, count in cur.execute("SELECT id, label, type, count FROM entity WHERE label IS NOT NULL"):
        base = BOOK_SUFFIX.sub("", label or "").strip()
        if typ not in (1, 2) or not base or not base[0].isupper() or any(c.isdigit() for c in base) or len(base) > 40 \
                or "," in base or " and " in base or re.search(r"(?:\A|\s)[A-Z]\.(?:\s|\Z)", base):
            continue
        e = byname.setdefault(base, {"ids": [], "count": 0, "kind": typ})
        e["ids"].append(eid)
        e["count"] += count or 0
        e["kind"] = min(e["kind"], typ)
    top = sorted(((n, e) for n, e in byname.items() if e["count"] >= MIN_MENTIONS), key=lambda t: -t[1]["count"])
    return top[:limit]


def annotate(xray: Path, log, index=None, client=None, workers=4, limit=MAX_CANDIDATES, people_from_dictionary=False) -> dict:
    """Put a pronunciation line at the top of the entries that need one. -> counts. Never raises.
    people_from_dictionary: a person's name is read from the dictionary only in a non-fiction book, where it is the real
    person; in a novel "Aram" is not the Aram of the Old Testament. Places and terms always may."""
    stats = {"names": 0, "dictionary": 0, "model": 0, "lines": 0, "rows": 0}
    try:
        conn = sqlite3.connect(xray)
        cur = conn.cursor()
        # a re-run starts clean: every line an earlier run wrote goes, including those of names that no longer qualify
        for eid, t in cur.execute("SELECT entity, text FROM entity_description").fetchall():
            if t and LINE_RE.match(t):
                cur.execute("UPDATE entity_description SET text = ? WHERE entity = ?", (LINE_RE.sub("", t), eid))
        cands = candidates(cur, limit)
        stats["names"] = len(cands)
        text = {eid: t for eid, t in cur.execute("SELECT entity, text FROM entity_description")}
        index = index or {}
        lines = {}                                       # name -> line

        base_of = {n: split_numeral(n) for n, _ in cands}                    # name -> (name without numeral, numeral)
        known = {n: dictionary_ipa(index, base_of[n][0]) for n, e in cands if people_from_dictionary or e["kind"] != 1}
        known = {n: v for n, v in known.items() if v}
        # the dictionary has Stanley and London too: the model says which names a reader would still stumble over
        shown = needs_help(client, sorted({base_of[n][0] for n in known}), workers) if client is not None \
            else {base_of[n][0] for n in known}
        for n in known:
            base, num = base_of[n]
            if base in shown:
                lines[n] = make_line(known[n] + (" " + ORDINALS[num][0] if num else ""), "", False)
                stats["dictionary"] += 1

        if client is not None:
            asked, todo = set(), []
            for n, e in cands:
                base = base_of[n][0]
                if n not in known and base not in asked:
                    asked.add(base)
                    todo.append((base, _hint(text.get(e["ids"][0], ""), e["kind"])))
            guesses = model_ipa(client, todo, workers)
            for n, _e in cands:
                base, num = base_of[n]
                if n in known or base not in guesses:
                    continue
                uk, us = guesses[base]
                if num:
                    uk, us = (uk + " " + ORDINALS[num][0]) if uk else "", (us + " " + ORDINALS[num][1]) if us else ""
                line = make_line(uk, us, True)
                if line:
                    lines[n] = line
                    stats["model"] += 1
        by = dict(cands)
        for n, line in lines.items():
            if not line:
                continue
            stats["lines"] += 1
            for eid in by[n]["ids"]:
                if eid in text and text[eid]:
                    cur.execute("UPDATE entity_description SET text = ? WHERE entity = ?",
                                (line + "\n" + LINE_RE.sub("", text[eid]), eid))
                    stats["rows"] += 1
        conn.commit()
        conn.close()
        log(f"  pronunciation: {stats['lines']} names of {stats['names']} considered ({stats['dictionary']} from the "
            f"dictionary, {stats['model']} model guesses marked ≈), {stats['rows']} entries written")
    except Exception as exc:                              # noqa: BLE001 - the X-Ray is valid without them
        log(f"  pronunciation skipped ({exc.__class__.__name__}: {exc})")
    return stats
