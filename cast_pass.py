"""Cast pass (v1.2.12): settle WHO the characters are before any card is written.

WordDumb finds names with spaCy and decides three things the cards then depend on:
  * what an entry is (people tab or terms tab) - from the label of the FIRST mention only, and spaCy calls Spanish and
    historical names organisations, places, nationalities and languages (Ursula: NORP 219, LANGUAGE 115, GPE 89);
  * which mentions exist - mentions tagged with a label WordDumb does not keep are lost (330 of Ursula's 509);
  * which entry a shared bare name belongs to - "Jose Arcadio" goes to whichever full name was seen first, so one entry
    held three men.
It also takes Wikipedia texts for any string it can match (the mushroom for "Morel", a name-origin page for "Fernanda").

This module repairs those four things on a COPY of the X-Ray, using the local model only where a rule cannot decide, and
only on a short list: a few dozen uncertain names, and the passages around a shared name. Everything is verified by code
(a model never writes an offset), and anything it cannot settle is left exactly as WordDumb made it.

Standard library only; every stage fails open (an error skips that stage, never the run).
"""
import bisect
import json
import os
import re
import shutil
import sqlite3
import time
import http.client
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TYPE_MIN_MENTIONS = 15        # a term must be mentioned this often to be considered for the people tab
WIKI_MIN_MENTIONS = 5         # a Wikipedia-described entry this often is checked against the book
TAGGED_MIN = 3                # WordDumb must have highlighted a name this often before the text is searched for it
MAX_CANDIDATES = 70           # at most this many names go to the model for typing / Wikipedia check
TYPE_BATCH = 15
WINDOWS_PER_NAME = 3
WINDOWS_WITH_WIKI = 5         # a Wikipedia text is only dropped on more evidence
ALIAS_MIN = 3                 # a highlighted surface form must occur this often to count as an alias
ALIAS_SHARE = 0.10            # ... and be this share of the entity's mentions (or be its label)
CONTINUATION_MAX = 0.25       # an alias followed by a capitalised word more often than this is part of longer names
SHARED_MIN_MENTIONS = 40      # bare mentions needed before a shared name is worth a model pass
SIBLING_MIN_MENTIONS = 3
SIBLING_SHARE = 0.05          # a longer name must have this share of the bare name's mentions to count as a sibling
MAX_SHARED_NAMES = 5
ANCHOR_STEP = 4               # ask every 4th bare mention of a shared name; the ones between inherit when both sides agree
DOMINANT = 0.9                # a character taking this share of the sample means the name is not really shared
MAX_ASSIGN_CALLS = 30         # hard cap on assignment requests per book (the time budget of the pass)
DISCOVERY_SAMPLE = 36
ASSIGN_BATCH = 30
MIN_CLUSTER = 10              # a new character needs this many mentions of the bare name
MAX_QUALIFIER_WORDS = 5

PARTS = {"de", "la", "del", "of", "the", "von", "van", "bin", "ibn", "al", "el", "y", "da", "di", "le", "du", "dos"}
TITLES = {"king", "queen", "prince", "princess", "emperor", "empress", "president", "colonel", "general", "captain",
          "major", "lord", "lady", "sir", "dame", "father", "pastor", "mister", "mr", "mrs", "miss", "doctor", "dr",
          "saint", "st", "don", "doña", "sister", "brother", "count", "baron", "duke", "pope", "bishop", "judge",
          "professor", "sergeant", "lieutenant", "commander", "admiral", "mayor", "governor", "senator", "sultan",
          "caliph", "emir", "sheikh", "shah", "tsar", "czar", "chief", "monsieur", "madame", "mademoiselle", "herr",
          "frau", "señor", "señora", "majesty", "excellency", "highness"}
ADDRESS_WORDS = {"papa", "mama", "mamma", "mom", "mum", "dad", "daddy", "mummy", "father", "mother", "uncle", "aunt", "grandpa",
                 "grandma", "grandfather", "grandmother", "brother", "sister", "son", "daughter", "child", "wife", "husband",
                 "sir", "madam", "master", "mistress", "lord", "lady", "papi", "mami", "mijo", "mija", "mijito", "mijita", "maman",
                 "mutti", "vati", "batya", "batyushka", "matushka", "dedushka", "babushka", "otosan", "okasan", "oniisan", "oneesan"}
_TOKEN = re.compile(r"^[A-ZÁÉÍÓÚÑÀ-ÖØ-Þ][\w’'\-\.]*$")
_TRAIL = " \t\n.,;:!?\"“”‘’'()[]—–-"
_CAPITAL_NEXT = re.compile(r"\s+[A-ZÁÉÍÓÚÑÀ-ÖØ-Þ]")

TYPE_SYSTEM = (
    "For each numbered name from a book, decide from the passages what it refers to: 'person' (an individual human or "
    "character, including one known by a first name only, a surname only or a nickname), 'place', 'group' (nationality, "
    "people, family, organisation, party, language), or 'other' (thing, event, work, concept, title). When a Wikipedia "
    "text is given, also say whether it describes the same person, place or thing as the passages do: 'match'; "
    "'mismatch' (a different person or thing that shares the name, a page about a given name or surname, or the "
    "dictionary sense of a word); or 'unsure'. Answer 'none' for wiki when no Wikipedia text is given. Use only the "
    "passages and the text shown.")
DISCOVER_SYSTEM = (
    "You build a character index for a book. Several different characters are referred to by the same bare name. From "
    "the numbered passages only, list the distinct characters that bare name refers to. A character is a person. Use "
    "the cues in the passages (family terms, titles, who is nearby, the period). Give each an id (A, B, C ...) and an "
    "identity of at most 12 words using only what the passages say, and person: true for a human character, false for a "
    "place, company or anything else that carries the same name. The same person at different ages or under a title "
    "is ONE character: tell different characters apart by their parents, family and role, never by age. If one of the "
    "characters is already known under a longer name (listed with an example passage), say so in same_as using that "
    "exact name, otherwise leave same_as empty. Also give the part of the book where the bare name refers to that "
    "character, as from_pct and to_pct (0-100, from the passages' positions). Ignore passages where the name is only "
    "mentioned as a name. At most 6 characters.")
ASSIGN_SYSTEM = (
    "Several characters share the bare name below. For each numbered passage say which character the name in [[double "
    "brackets]] refers to. Answer with a character id (A, B ...), or with the exact longer name if the passage means "
    "one of the already known characters, 'none' if the passage only mentions the name without meaning a person, or "
    "'unsure' if the passage does not decide it. The bracketed name is a short form: when the same passage also gives a "
    "longer name (\"Colonel Aureliano Buendia\"), that is usually ANOTHER person than the bracketed one, so do not "
    "answer with it just because it is mentioned. Answer with a known character's name only when the bracketed name "
    "itself plainly means that person; otherwise choose among the characters above, or 'unsure'. Use the passage and "
    "the position in the book; do not guess.")


def _schema(props):
    return {"type": "object", "properties": {"entries": {"type": "array", "items": {
        "type": "object", "properties": props, "required": list(props), "additionalProperties": False}}},
        "required": ["entries"], "additionalProperties": False}


TYPE_SCHEMA = _schema({"i": {"type": "integer"},
                       "kind": {"type": "string", "enum": ["person", "place", "group", "other"]},
                       "wiki": {"type": "string", "enum": ["match", "mismatch", "unsure", "none"]}})
DISCOVER_SCHEMA = _schema({"id": {"type": "string"}, "identity": {"type": "string"}, "person": {"type": "boolean"},
                           "same_as": {"type": "string"}, "from_pct": {"type": "integer"}, "to_pct": {"type": "integer"}})
ASSIGN_SCHEMA = _schema({"i": {"type": "integer"}, "who": {"type": "string"}})
_KIN = (r"\b(?:son|daughter|wife|husband|brother|sister|father|mother|grandson|granddaughter|grandfather|grandmother|"
        r"nephew|niece|cousin|uncle|aunt|widow|heir|child|children|namesake)")
_PAIR = re.compile(r"(\d+)\s*[:=]\s*([A-Za-z]\w{0,3})")


# --------------------------------------------------------------------------
# text helpers (no model, no database)
# --------------------------------------------------------------------------

def book_text(positions, texts) -> str:
    """The book as one string whose index is the KFX position (gaps between entries are NUL)."""
    if not positions:
        return ""
    out = ["\0"] * (positions[-1] + len(texts[-1]) + 2)
    for p, t in zip(positions, texts):
        out[p:p + len(t)] = list(t)
    return "".join(out)


def proper_name(label: str) -> bool:
    """'Ursula', 'Colonel Aureliano Buendia', 'Santa Sofia de la Piedad'; not 'the king', 'EU', '1914'."""
    toks = (label or "").split()
    if not toks or len(label) < 3 or not _TOKEN.match(toks[0]) or not _TOKEN.match(toks[-1]):
        return False
    if len(toks) == 1 and label.lower() in ADDRESS_WORDS:
        return False                                   # "Papa", "Mother": a way of addressing someone, not a name
    return all(_TOKEN.match(t) or t in PARTS for t in toks)


def find_spans(text: str, label: str):
    pat = re.compile(r"(?<![\w’'\-])" + re.escape(label) + r"(?![\w\-])")
    return [(m.start(), m.end()) for m in pat.finditer(text)]


def selectable_len(text: str, start: int, end: int) -> int:
    """WordDumb highlights the name plus one following punctuation mark so Kindle can select it."""
    return (end - start) + (1 if re.match(r"[^\w\s]", text[end:end + 1] or " ") else 0)


def surface_of(text: str, start: int, length: int) -> str:
    return text[start:start + length].strip(_TRAIL)


def entry_bounds(text: str, p: int):
    lo = text.rfind("\0", 0, p) + 1
    hi = text.find("\0", p)
    return lo, (len(text) if hi < 0 else hi)


def window(text: str, p: int, name: str, before: int = 200, after: int = 130) -> str:
    lo, hi = entry_bounds(text, p)
    s = text[max(lo, p - before):p].replace("\n", " ")
    e = text[p + len(name):min(hi, p + len(name) + after)].replace("\n", " ")
    return f"...{s}[[{name}]]{e}..."


def sentence_at(text: str, p: int, limit: int = 300) -> str:
    lo, hi = entry_bounds(text, p)
    a = max(lo, p - limit)
    seg = text[a:p]
    cut = max(seg.rfind(". "), seg.rfind("? "), seg.rfind("! "), seg.rfind("\n"))
    a = a + cut + 2 if cut >= 0 else a
    seg2 = text[p:min(hi, p + limit)]
    m = re.search(r"[.?!](?:\s|$)", seg2)
    b = p + (m.end() if m else len(seg2))
    return " ".join(text[a:b].split())


class Claims:
    """Sorted interval set with an overlap test (used so that new highlights never overlap existing ones)."""

    def __init__(self, intervals=()):
        self.iv = sorted(intervals)
        self._index()

    def _index(self):
        self.starts = [s for s, _ in self.iv]
        self.maxend = []
        m = -1
        for _, e in self.iv:
            m = max(m, e)
            self.maxend.append(m)

    def overlaps(self, s: int, e: int) -> bool:
        i = bisect.bisect_left(self.starts, e)
        return i > 0 and self.maxend[i - 1] > s

    def add(self, s: int, e: int):
        i = bisect.bisect_left(self.starts, s)
        self.iv.insert(i, (s, e))
        self.starts.insert(i, s)
        self.maxend.insert(i, 0)
        m = self.maxend[i - 1] if i else -1
        for j in range(i, len(self.iv)):
            m = max(m, self.iv[j][1])
            self.maxend[j] = m


def continuation_rate(text: str, spans) -> float:
    """Share of mentions followed by another capitalised word ('King' -> 'King Leopold', 'King Alfonso')."""
    if not spans:
        return 0.0
    return sum(bool(_CAPITAL_NEXT.match(text[e:e + 30])) for _, e in spans) / len(spans)


def tokens_contain(longer: str, shorter: str) -> bool:
    """The shorter name is the first or the last word(s) of the longer one: "Aureliano" / "Aureliano Segundo" (a given
    name), "Stanley" / "Henry Morton Stanley" (a surname). A word in the middle ("Arcadio" in "Jose Arcadio Buendia") is
    not enough: that is part of a compound given name, not a short form of it."""
    a, b = longer.split(), shorter.split()
    return len(a) > len(b) and (a[:len(b)] == b or a[-len(b):] == b)


def settle_outliers(targets: dict, n: int) -> int:
    """targets: {mention position: target}. A character whose mentions sit in one stretch of the book (75% within a
    quarter of the book of their median) is not likely to turn up, as the bare name, more than 45% of the book away:
    such a mention goes to the one other character whose own stretch (10th-90th percentile) covers it, otherwise it
    is left as it was. Returns how many were changed."""
    by = defaultdict(list)
    for p, t in targets.items():
        by[t].append(p)
    big = {t: sorted(ps) for t, ps in by.items() if len(ps) >= 10}
    if len(big) < 2:
        return 0
    stretch = {t: (ps[len(ps) // 10], ps[-1 - len(ps) // 10]) for t, ps in big.items()}
    changed = 0
    for t, ps in big.items():
        med = ps[len(ps) // 2]
        if sum(abs(p - med) <= 0.25 * n for p in ps) < 0.75 * len(ps):
            continue
        for p in ps:
            if abs(p - med) <= 0.45 * n:
                continue
            inside = [u for u, (a, b) in stretch.items() if u != t and a <= p <= b]
            if len(inside) == 1:
                targets[p] = inside[0]
            else:
                del targets[p]
            changed += 1
    return changed


_NUMERAL = re.compile(r"^(?:[IVX]{1,5}|Jr\.?|Sr\.?)$")


def core_tokens(label: str) -> list:
    """The name without titles and regnal numerals: 'King Leopold' and 'Leopold II' both come out as 'Leopold'."""
    return [t for t in label.split() if t.lower().strip(".") not in TITLES and not _NUMERAL.match(t)]


def distinct_siblings(bare: str, labels) -> list:
    """The longer names that could be a different person with this short name. A name that adds only a title or a
    numeral is the same person as the bare name; a name whose words are all inside another longer name is an alias of
    that one. What is left are names that differ from each other ('Aureliano Segundo', 'Aureliano Jose')."""
    b = core_tokens(bare)
    longer = []
    for lab in labels:
        c = core_tokens(lab)
        if len(c) > len(b) and (c[:len(b)] == b or c[-len(b):] == b):
            longer.append((lab, set(t.lower().strip(".") for t in c)))
    out, seen = [], []
    for lab, toks in longer:
        if toks in seen:
            continue                                   # same words as one already kept (a title makes the difference)
        if not any(toks < other for _l, other in longer):
            out.append(lab)
            seen.append(toks)
    return out


def trim_lonely_start(ps: list, n: int, gap: float = 0.10, keep: int = 3) -> list:
    """A new character's first card is written from its earliest mentions, so one stray mention far before the others
    (a passage about someone else's twin at 63 % of the book, the rest from 81 %) would give it the wrong first page.
    The leading mentions that sit more than `gap` of the book before the next one are dropped (they stay where they
    were). ps: sorted positions."""
    ps = list(ps)
    while len(ps) > keep and ps[1] - ps[0] > gap * n:
        ps.pop(0)
    return ps


def clean_qualifier(q: str) -> str:
    q = re.sub(r"[\(\)\[\]\d]", " ", q or "")
    q = " ".join(q.split()).strip(" .,;:-\"'")
    return " ".join(q.split()[:MAX_QUALIFIER_WORDS])


# --------------------------------------------------------------------------
# the local model (same request conventions as enrich.py: schema on, tools off, no thinking)
# --------------------------------------------------------------------------

class Client:
    def __init__(self, base, model, key=None, timeout=240):
        self.base, self.model, self.timeout = base.rstrip("/"), model, timeout
        self.headers = {"Content-Type": "application/json"}
        if key:
            self.headers["Authorization"] = "Bearer " + key
        self.calls = 0
        self.failed = 0
        self.dead = False
        self.prefix = ""          # the book brief, put in front of every user message

    def ask(self, system, user, schema, retries=2):
        if self.dead:
            return None
        body = {"model": self.model, "temperature": 0, "max_tokens": 3500,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": self.prefix + user}],
                "chat_template_kwargs": {"enable_thinking": False}, "tool_choice": "none",
                "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": schema, "strict": True}}}
        for attempt in range(retries + 1):
            req = urllib.request.Request(self.base + "/chat/completions", json.dumps(body).encode("utf-8"), self.headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    resp = json.load(r)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 404):
                    self.dead = True
                    return None
                time.sleep((5, 20, 45)[min(attempt, 2)] if retries else 0)
                continue
            except urllib.error.URLError:
                self.dead = True
                return None
            except (OSError, ValueError, http.client.HTTPException):
                continue
            txt = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            try:
                parsed = json.loads(txt)
            except ValueError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("entries"), list):
                self.calls += 1
                return parsed["entries"]
        self.failed += 1
        return None


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------

def _read(cur):
    ents = {}
    for i, label, loc, typ, cnt, has in cur.execute(
            "SELECT id, label, loc_label, type, count, has_info_card FROM entity WHERE label IS NOT NULL AND type IS NOT NULL"):
        ents[i] = {"id": i, "label": label, "loc": loc, "type": typ, "count": cnt or 0, "has": has,
                   "source": None, "desc": None}
    for text, source, ent in cur.execute("SELECT text, source, entity FROM entity_description"):
        if ent in ents:
            ents[ent]["source"], ents[ent]["desc"] = source, text
    occ = defaultdict(list)
    for ent, s, ln in cur.execute("SELECT entity, start, length FROM occurrence"):
        occ[ent].append((s, ln))
    return ents, occ


def _spread(items, k):
    if len(items) <= k:
        return list(items)
    step = (len(items) - 1) / (k - 1) if k > 1 else 0
    return [items[round(i * step)] for i in range(k)]


def run(src: Path, dest: Path, positions, texts, log, opts, is_citation=lambda s: False, key=None, is_fictional=lambda t: False):
    """Write the repaired copy of the X-Ray at `dest`. -> report dict, or None when nothing could be done.
    Never raises: a failing stage is logged and skipped."""
    t0 = time.time()
    try:
        text = book_text(positions, texts)
        shutil.copy2(src, dest)
    except Exception as exc:                      # noqa: BLE001
        log(f"  cast pass skipped ({exc.__class__.__name__}: {exc})")
        return None
    conn = sqlite3.connect(dest)
    cur = conn.cursor()
    ents, occ = _read(cur)
    model = getattr(opts, "model_high", "") or getattr(opts, "model_bulk", "")
    client = Client(opts.base_url, model, key)
    client.prefix = getattr(opts, "brief_full", "") or ""
    parallel = max(1, int(getattr(opts, "parallel", 4) or 4))
    report = {"retyped": [], "wiki_rejected": [], "mentions_added": {}, "split": {}, "locked": [], "seconds": 0}

    by_label = defaultdict(list)
    for e in ents.values():
        by_label[e["label"]].append(e["id"])
    span_cache = {}
    all_labels = [lab for lab in by_label if proper_name(lab)]

    def spans_of(label):
        if label not in span_cache:
            span_cache[label] = find_spans(text, label)
        return span_cache[label]

    def own_spans(label):
        """The mentions of `label` that are not part of a longer name some other entry already has ("France" inside
        "Anatole France", "Leopold" inside "King Leopold")."""
        longer = [lab for lab in all_labels if tokens_contain(lab, label)]
        if not longer:
            return spans_of(label)
        inside = Claims(sp for lab in longer for sp in spans_of(lab))
        return [sp for sp in spans_of(label) if not inside.overlaps(*sp)]

    def prose_windows(label, k=WINDOWS_PER_NAME):
        ws = []
        for s, e in _spread(own_spans(label), k * 3):
            w = window(text, s, label, 110, 70)
            if not is_citation(w):
                ws.append(w)
            if len(ws) >= k:
                break
        return ws

    stamps = [("start", time.time())]
    try:
        _stage_typing(cur, text, ents, occ, by_label, own_spans, prose_windows, client, report, log)
    except Exception as exc:                      # noqa: BLE001
        log(f"  cast pass: typing stage skipped ({exc.__class__.__name__}: {exc})")
    stamps.append(("typing", time.time()))
    try:
        _stage_wiki_bare(cur, text, ents, occ, client, is_citation, is_fictional, report, log)
    except Exception as exc:                      # noqa: BLE001
        log(f"  cast pass: Wikipedia-owned bare names skipped ({exc.__class__.__name__}: {exc})")
    stamps.append(("wiki bare", time.time()))
    try:
        shared = find_shared(text, ents, occ, spans_of)
        only = getattr(opts, "cast_only", None)          # debugging aid: restrict to one shared name
        if only:
            shared = [x for x in shared if x[0] == only]
    except Exception as exc:                      # noqa: BLE001
        shared = []
        log(f"  cast pass: shared-name search skipped ({exc.__class__.__name__}: {exc})")
    stamps.append(("find shared", time.time()))
    try:
        _stage_gapfill(cur, text, ents, occ, by_label, spans_of, is_citation, report, log, skip={b for b, _ in shared})
    except Exception as exc:                      # noqa: BLE001
        log(f"  cast pass: mention recovery skipped ({exc.__class__.__name__}: {exc})")
    stamps.append(("mention recovery", time.time()))
    try:
        _stage_shared(cur, text, ents, occ, by_label, spans_of, client, parallel, is_citation, report, log, shared)
    except Exception as exc:                      # noqa: BLE001
        log(f"  cast pass: shared-name stage skipped ({exc.__class__.__name__}: {exc})")

    stamps.append(("shared names", time.time()))
    report["stage_seconds"] = {a: round(b[1] - a_[1], 1) for (a_, b) in zip(stamps, stamps[1:]) for a in [b[0]]}
    emptied = [i for i in report.get("_emptied", []) if i]
    if emptied:                                   # an entry whose every mention went to another character
        cur.execute("DELETE FROM entity WHERE id IN (%s) AND id NOT IN (SELECT entity FROM occurrence)"
                    % ",".join(str(i) for i in emptied))
    cur.execute("UPDATE entity SET count = (SELECT count(*) FROM occurrence WHERE occurrence.entity = entity.id) "
                "WHERE label IS NOT NULL")
    cur.execute("DELETE FROM entity_description WHERE entity NOT IN (SELECT id FROM entity)")
    conn.commit()
    bad = cur.execute("SELECT count(*) FROM occurrence WHERE entity NOT IN (SELECT id FROM entity)").fetchone()[0]
    conn.close()
    if bad:
        dest.unlink(missing_ok=True)
        log(f"  cast pass discarded: {bad} orphaned occurrences")
        return None
    report.pop("_emptied", None)
    report["seconds"] = round(time.time() - t0, 1)
    report["calls"] = client.calls
    changed = (report["retyped"] or report["wiki_rejected"] or report["mentions_added"] or report["split"] or report.get("wiki_bare"))
    log(f"  cast pass: {client.calls} model calls, {report['seconds']} s"
        + ("" if changed else "; nothing to change"))
    return report if changed else None


def _stage_typing(cur, text, ents, occ, by_label, spans_of, prose_windows, client, report, log):
    """People tab and Wikipedia check: retype terms the model says are people; drop Wikipedia texts that describe
    something else with the same name. Only an explicit 'mismatch' removes a text."""
    cands = []
    for label, ids in by_label.items():
        if not proper_name(label):
            continue
        rows = [ents[i] for i in ids]
        is_term = all(r["type"] == 2 for r in rows)
        wiki_row = next((r for r in rows if r["source"] is not None and r["desc"]), None)
        tagged = sum(len(occ.get(i, [])) for i in ids)
        n = len(spans_of(label)) if (tagged >= TAGGED_MIN and (is_term or wiki_row)) else 0
        if (is_term and n >= TYPE_MIN_MENTIONS) or (wiki_row and n >= WIKI_MIN_MENTIONS):
            cands.append((n, label, is_term, wiki_row["desc"] if wiki_row else None))
    cands.sort(key=lambda c: -c[0])
    cands = cands[:MAX_CANDIDATES]
    work = []
    for n, label, is_term, wiki in cands:
        ws = prose_windows(label, WINDOWS_WITH_WIKI if wiki else WINDOWS_PER_NAME)
        if ws:
            work.append((label, is_term, wiki, ws))
    if not work:
        return
    answers = {}
    for k in range(0, len(work), TYPE_BATCH):
        chunk = work[k:k + TYPE_BATCH]
        blocks = []
        for i, (label, _t, wiki, ws) in enumerate(chunk):
            b = f"[{i}] {label}\n" + "\n".join("  - " + w for w in ws)
            if wiki:
                b += "\n  Wikipedia text: " + " ".join(wiki.split())[:240]
            blocks.append(b)
        res = client.ask(TYPE_SYSTEM, "\n\n".join(blocks), TYPE_SCHEMA)
        for e in res or []:
            if isinstance(e.get("i"), int) and 0 <= e["i"] < len(chunk):
                answers[chunk[e["i"]][0]] = (e.get("kind"), e.get("wiki"))
    for label, is_term, wiki, ws in work:
        kind, wk = answers.get(label, (None, None))
        if is_term and kind == "person":
            for i in by_label[label]:
                if ents[i]["type"] == 2:
                    cur.execute("UPDATE entity SET type = 1 WHERE id = ?", (i,))
                    ents[i]["type"] = 1
            report["retyped"].append(label)
        if wiki and wk == "mismatch":
            first = spans_of(label)[0][0] if spans_of(label) else None
            fallback = sentence_at(text, first) if first is not None else ""
            for i in by_label[label]:
                if ents[i]["source"] is not None and fallback:
                    cur.execute("UPDATE entity_description SET text = ?, source = NULL WHERE entity = ?", (fallback, i))
                    ents[i]["source"], ents[i]["desc"] = None, fallback
            report["wiki_rejected"].append(label)
    if report["retyped"]:
        names = report["retyped"]
        log(f"  cast pass: {len(names)} entries moved to the people tab: {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")
    if report["wiki_rejected"]:
        names = report["wiki_rejected"]
        log(f"  cast pass: {len(names)} Wikipedia texts describe something else with the same name and are not used: "
            f"{', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")


def find_shared(text, ents, occ, spans_of):
    """[(bare, [longer person labels containing it])] for every name (a label or a highlighted surface form of a
    person) that two or more longer names of people contain: "Aureliano", "Jose Arcadio", "Amaranta"."""
    people = {i: e for i, e in ents.items() if e["type"] == 1}
    plabels = defaultdict(list)
    for i, e in people.items():
        plabels[e["label"]].append(i)
    pool = set(plabels)
    for i in people:
        seen = Counter(surface_of(text, s, ln) for s, ln in occ.get(i, []))
        pool.update(sf for sf, c in seen.items() if c >= ALIAS_MIN and proper_name(sf))
    weight = {lab: sum(len(occ.get(j, [])) for j in js) for lab, js in plabels.items()}
    out = []
    for b in pool:
        if not proper_name(b) or " " not in b and b.lower() in TITLES:
            continue
        floor = max(SIBLING_MIN_MENTIONS, SIBLING_SHARE * len(spans_of(b)))
        sibs = distinct_siblings(b, [lab for lab in plabels if lab != b and weight[lab] >= floor])
        if len(sibs) >= 2:
            out.append((b, sorted(sibs)))
    out.sort(key=lambda x: -len(spans_of(x[0])))
    return out[:MAX_SHARED_NAMES]


WIKI_BARE_MIN = 3             # bare mentions an entry with a Wikipedia text must hold before they are checked
WIKI_BARE_MAX = 80            # more than this and the bare name is the entry's usual name (a main character), not a stray
WIKI_BARE_SHARE = 0.25
WIKI_BARE_ENTRIES = 12          # entries checked per book: the ones whose mentions are mostly bare first
WIKI_LEAD_CHARS = 300
WIKIBARE_SYSTEM = (
    "A Wikipedia entry is attached to a name in a book, but many mentions of the bare first name or surname in the book may "
    "be someone else (another person with that name, a pet, an author named in a footnote). You get the entry's name, the "
    "opening of its Wikipedia text, and numbered passages with the bare name in [[double brackets]]. For each passage answer "
    "with: the entry's name if the passage is about that very person or figure (judge by what the passage says: the period, "
    "the deeds, the place); the exact name of one of the other people listed if it is that one; 'other' if it is someone or "
    "something else (a different man called John, an animal, an author in a citation); or 'unsure'. Do not guess from the "
    "name alone: a boy at a workhouse is not a medieval king.")


def _stage_wiki_bare(cur, text, ents, occ, client, is_citation, is_fictional, report, log):
    """An entry that carries a Wikipedia text ("Prester John") can hold mentions of the bare name ("John") that WordDumb merged
    into it because the full name was seen first: in King Leopold's Ghost 13 of its 16 mentions were young John Rowlands,
    Stanley's bulldog, the missionary John Harris and two authors in the bibliography. Each such mention is put to the model
    with the opening of the Wikipedia text: it stays, moves to another person of that first name (only inside the stretch of
    the book where that person's full name is found), or is taken off the entry (no highlight rather than the wrong
    encyclopedia article). Only for entries whose Wikipedia text is kept (not a fictional character's page), and only when
    the bare mentions are a minority-sized stray (3 to 80): a main character's usual name is left alone."""
    cands = []
    for e in ents.values():
        if e["type"] != 1 or e["source"] is None or not e["desc"] or is_fictional(e["desc"][:300]) or " " not in e["label"]:
            continue
        toks = set(e["label"].split())
        mine = occ.get(e["id"], [])
        bare = [(s, ln) for s, ln in mine if surface_of(text, s, ln) in toks and " " not in surface_of(text, s, ln)]
        if WIKI_BARE_MIN <= len(bare) <= WIKI_BARE_MAX and len(bare) >= WIKI_BARE_SHARE * len(mine):
            cands.append((len(bare) / max(1, len(mine)), e, bare))
    cands.sort(key=lambda c: (-c[0], -len(c[2])))
    if not cands:
        return
    people = [x for x in ents.values() if x["type"] == 1]
    n = len(text) or 1
    summary = {}
    for _share, e, bare in cands[:WIKI_BARE_ENTRIES]:
        sib = sorted({x["label"] for x in people if x["id"] != e["id"] and proper_name(x["label"])
                      and any(tokens_contain(x["label"], surface_of(text, s, ln)) for s, ln in bare[:1])
                      and len(occ.get(x["id"], [])) >= 3},
                     key=lambda lab: -sum(len(occ.get(x["id"], [])) for x in people if x["label"] == lab))[:6]
        sib_ids = {lab: max((x for x in people if x["label"] == lab), key=lambda x: len(occ.get(x["id"], []))) for lab in sib}
        known = "\n".join(f"- {lab}: {window(text, occ[sib_ids[lab]['id']][0][0], lab, 80, 50)}" for lab in sib if occ.get(sib_ids[lab]["id"]))
        lead = " ".join(e["desc"].split())[:WIKI_LEAD_CHARS]
        verdict = {}
        bare = sorted(bare)[:90]
        for k in range(0, len(bare), 30):
            chunk = bare[k:k + 30]
            user = (f"Entry: {e['label']}\nWikipedia text: {lead}\nOther people with this first name or surname in the book:\n"
                    f"{known or '(none)'}\n\nPassages:\n"
                    + "\n\n".join(f"[{i}] ({100 * s / n:.0f}% into the book) {window(text, s, surface_of(text, s, ln))}"
                                  for i, (s, ln) in enumerate(chunk)))
            res = client.ask(WIKIBARE_SYSTEM, user, ASSIGN_SCHEMA)
            for r in res or []:
                if isinstance(r.get("i"), int) and 0 <= r["i"] < len(chunk):
                    verdict[chunk[r["i"]][0]] = str(r.get("who", "")).strip()
        moved, detached, kept = Counter(), 0, 0
        for s, ln in bare:
            who = verdict.get(s, "")
            if not who or who.lower() == "unsure" or who == e["label"] or who.lower() == e["label"].lower():
                kept += 1
                continue
            target = sib_ids.get(who)
            if target is not None:
                sp = sorted(a for a, _b in find_spans(text, who))
                ok = len(sp) >= 3 and sp[len(sp) // 20] - 0.05 * n <= s <= sp[-1 - len(sp) // 20] + 0.05 * n
                if ok:
                    cur.execute("UPDATE occurrence SET entity = ? WHERE start = ? AND entity = ?", (target["id"], s, e["id"]))
                    occ[e["id"]].remove((s, ln)); occ[target["id"]].append((s, ln))
                    moved[who] += 1
                    continue
            cur.execute("DELETE FROM occurrence WHERE start = ? AND entity = ?", (s, e["id"]))
            occ[e["id"]].remove((s, ln))
            detached += 1
        if moved or detached:
            summary[e["label"]] = {"checked": len(bare), "kept": kept, "moved": dict(moved), "detached": detached}
            log(f"  cast pass: '{e['label']}' (Wikipedia text) held {len(bare)} bare mentions: {kept} stay, "
                + (f"{sum(moved.values())} moved ({', '.join(f'{k} {v}' for k, v in moved.items())}), " if moved else "")
                + f"{detached} taken off the entry")
    if summary:
        report["wiki_bare"] = summary


def _stage_gapfill(cur, text, ents, occ, by_label, spans_of, is_citation, report, log, skip=()):
    """Find the mentions of people that WordDumb dropped (spaCy called them a nationality, a language, ...): exact
    matches of a name the book already highlights for that person, never overlapping an existing highlight."""
    people = {i: e for i, e in ents.items() if e["type"] == 1}
    owners = defaultdict(Counter)                      # surface -> entity -> count
    for i, e in people.items():
        total = len(occ.get(i, []))
        if total < ALIAS_MIN:
            continue
        for s, ln in occ[i]:
            sf = surface_of(text, s, ln)
            if proper_name(sf):
                owners[sf][i] += 1
    alias_owner = {}
    for sf, cnt in owners.items():
        if sf in skip:
            continue
        labels = {people[i]["label"] for i in cnt}
        if len(labels) != 1:
            continue                                   # a shared name: the shared-name stage decides, not an exact match
        owner = max(cnt, key=lambda i: (len(occ[i]), -i))
        n, total = sum(cnt.values()), sum(len(occ[i]) for i in by_label[people[owner]["label"]] if i in occ)
        if n < ALIAS_MIN or not (n >= ALIAS_SHARE * total or sf == people[owner]["label"]):
            continue
        if " " not in sf and sf.lower() in TITLES:
            continue
        if " " not in sf and len(distinct_siblings(sf, [lab for lab in by_label if lab != sf and proper_name(lab)])) >= 2:
            continue                                   # "John": John Rowlands, John Harris, Prester John ... which one?
        if continuation_rate(text, spans_of(sf)) > CONTINUATION_MAX and " " not in sf:
            continue
        alias_owner[sf] = owner
    # every longer name of any kind claims its own mentions first, so "Stanley" never takes "Stanley Pool"
    claimers = set(alias_owner)
    for e in ents.values():
        if proper_name(e["label"]) and " " in e["label"] and len(occ.get(e["id"], [])) >= 2:
            claimers.add(e["label"])
    claims = Claims((s, s + ln) for lst in occ.values() for s, ln in lst)
    added = defaultdict(list)
    for name in sorted(claimers, key=lambda n: (-len(n.split()), -len(n))):
        for s, e in spans_of(name):
            if claims.overlaps(s, e):
                continue
            ln = selectable_len(text, s, e)
            if claims.overlaps(s, s + ln):
                ln = e - s
            claims.add(s, s + ln)
            if name in alias_owner and not is_citation(window(text, s, name, 120, 120)):
                added[alias_owner[name]].append((s, ln))
    total = 0
    for eid, lst in added.items():
        cur.executemany("INSERT INTO occurrence (entity, start, length) VALUES (?, ?, ?)", [(eid, s, ln) for s, ln in lst])
        occ[eid].extend(lst)
        report["mentions_added"][people[eid]["label"]] = report["mentions_added"].get(people[eid]["label"], 0) + len(lst)
        total += len(lst)
    if total:
        top = sorted(report["mentions_added"].items(), key=lambda kv: -kv[1])[:5]
        log(f"  cast pass: {total} mentions recovered for {len(report['mentions_added'])} people "
            f"(names WordDumb had dropped as nationalities, languages or places): "
            + ", ".join(f"{n} +{c}" for n, c in top))


def _stage_shared(cur, text, ents, occ, by_label, spans_of, client, parallel, is_citation, report, log, shared):
    """A bare name that two or more longer names of people contain ("Aureliano", "Jose Arcadio") is shared. Its bare
    mentions are sorted into characters by the model, each from the passage around it."""
    people = {i: e for i, e in ents.items() if e["type"] == 1}
    plabels = defaultdict(list)
    for i, e in people.items():
        plabels[e["label"]].append(i)
    if not shared:
        return
    claims_all = Claims((s, s + ln) for lst in occ.values() for s, ln in lst)
    emptied = set()
    budget = MAX_ASSIGN_CALLS
    checked = report.setdefault("checked", {})
    for bare, sibs in shared:
        every_longer = [lab for lab in by_label if proper_name(lab) and tokens_contain(lab, bare)]
        sib_spans = Claims(s_e for lab in every_longer for s_e in spans_of(lab))
        mentions = [s for s, e in spans_of(bare) if not sib_spans.overlaps(s, e)
                    and not is_citation(window(text, s, bare, 120, 120))]
        if len(mentions) < SHARED_MIN_MENTIONS:
            checked[bare] = f"only {len(mentions)} bare mentions"
            continue
        n = len(text) or 1
        known = []
        for lab in sibs:
            sp = spans_of(lab)
            known.append((lab, window(text, sp[0][0], lab, 90, 60) if sp else ""))
        sample = _spread(mentions, DISCOVERY_SAMPLE)
        user = (f"Bare name: {bare}\nAlready known characters with a longer name:\n"
                + "\n".join(f"- {lab}: {w}" for lab, w in known) + "\n\nPassages:\n"
                + "\n\n".join(f"[{i}] ({100 * p / n:.0f}% into the book) {window(text, p, bare)}"
                              for i, p in enumerate(sample)))
        disc = client.ask(DISCOVER_SYSTEM, user, DISCOVER_SCHEMA)
        if not disc:
            checked[bare] = "no answer"
            continue
        chars = {}
        for c in disc[:6]:
            cid = str(c.get("id", "")).strip().upper()[:3]
            if c.get("person") is False:
                continue                               # a place, a company ...: its mentions are left alone
            if cid and not cid.startswith("K") and cid not in ("N", "U"):
                same = c.get("same_as", "")
                ident = c.get("identity", "")
                if same and re.search(_KIN + r"\b[^.;]{0,30}" + re.escape(same), ident, re.I):
                    same = ""          # "son of X" is not X: a person's identity never names himself as his own kin
                chars[cid] = {"identity": ident, "same_as": same if same in sibs else "",
                              "from": c.get("from_pct"), "to": c.get("to_pct")}
        if len(chars) < 2 and not any(c["same_as"] for c in chars.values()):
            checked[bare] = f"{len(mentions)} bare mentions, one character"
            continue                                   # one character (or none): the name is not shared after all
        cl = "\n".join(f"{cid}: {c['identity']}" + (f" (the same as {c['same_as']})" if c["same_as"] else "")
                       + (f" (referred to between {c['from']}% and {c['to']}% of the book)"
                          if isinstance(c.get("from"), int) and isinstance(c.get("to"), int) else "")
                       for cid, c in chars.items())
        cl += "\nAlready known (answer with the exact name): " + "; ".join(sibs)
        def assign(chunk):
            u = ("Characters:\n" + cl + "\n\nPassages:\n"
                 + "\n\n".join(f"[{i}] ({100 * p / n:.0f}% into the book) {window(text, p, bare)}"
                               for i, p in enumerate(chunk)))
            return chunk, client.ask(ASSIGN_SYSTEM, u, ASSIGN_SCHEMA)

        who = {}

        def collect_answers(results_):
            for chunk, res in results_:
                for e in res or []:
                    if isinstance(e.get("i"), int) and 0 <= e["i"] < len(chunk):
                        who[chunk[e["i"]]] = str(e.get("who", "")).strip()

        # First the sample the characters were found from: when one character takes nearly all of it, the name is
        # not really shared in this book and the rest is left alone (saves most of the model time on such names).
        collect_answers([assign(sample)])
        top = Counter(who.get(p) for p in sample if who.get(p))
        if top and top.most_common(1)[0][1] >= DOMINANT * len(sample):
            checked[bare] = f"{len(mentions)} bare mentions, {top.most_common(1)[0][0]} takes nearly all"
            continue
        # Every ANCHOR_STEP-th mention is asked; a mention between two anchors that got the same character
        # inherits it (the characters of a shared name hold long stretches of the book), and the mentions between
        # two anchors that disagree - the places where one character gives way to another - are asked too.
        order = sorted(mentions)
        anchors = [p for i, p in enumerate(order) if (i % ANCHOR_STEP == 0 or i == len(order) - 1) and p not in who]
        a_batches = [anchors[k:k + ASSIGN_BATCH] for k in range(0, len(anchors), ASSIGN_BATCH)]
        if len(a_batches) > budget:
            checked[bare] = "no call budget left"
            continue                                   # not enough call budget left for this name: leave it as it is
        budget -= len(a_batches)
        with ThreadPoolExecutor(max_workers=parallel) as ex:
            collect_answers(list(ex.map(assign, a_batches)))
        settled = lambda w: bool(w) and w.lower() not in ("none", "unsure", "n", "u")
        answered = [p for p in order if p in who]
        boundary = []
        for lo, hi in zip(answered, answered[1:]):
            between = [p for p in order if lo < p < hi]
            if between and settled(who[lo]) and who[lo] == who[hi]:
                for p in between:
                    who[p] = who[lo]
            else:
                boundary.extend(between)
        boundary = [p for p in boundary if p not in who]
        b_batches = [boundary[k:k + ASSIGN_BATCH] for k in range(0, len(boundary), ASSIGN_BATCH)]
        b_batches = b_batches[:max(0, budget)]
        budget -= len(b_batches)
        if b_batches:
            with ThreadPoolExecutor(max_workers=parallel) as ex:
                collect_answers(list(ex.map(assign, b_batches)))

        # A character may only be merged into a known one (by "same as", or by the model naming it) where that known
        # character's own full name is found: inside the stretch of the book its mentions cover. The model's word is
        # not enough (it called the last Aureliano "the same as Aureliano Segundo").
        def stretch(label):
            sp = sorted(a for a, _b in spans_of(label))
            if len(sp) < 3:
                return None
            return sp[len(sp) // 20] - 0.05 * n, sp[-1 - len(sp) // 20] + 0.05 * n

        by_who = defaultdict(list)
        for p_, w_ in who.items():
            by_who[w_.upper()].append(p_)
        for cid, c in chars.items():
            if c["same_as"]:
                ps = sorted(by_who.get(cid, []))
                st = stretch(c["same_as"])
                if not (ps and st and st[0] <= ps[len(ps) // 2] <= st[1]):
                    c["same_as"] = ""

        def target_of(w):
            if w in sibs:
                return ("known", w)
            cid = w.upper()
            c = chars.get(cid)
            if c is None:
                return None
            return ("known", c["same_as"]) if c["same_as"] else ("new", cid)

        targets = {p: target_of(w) for p, w in who.items()}
        targets = {p: t for p, t in targets.items() if t is not None}
        for p_, t_ in list(targets.items()):
            if t_[0] == "known" and who.get(p_) in sibs:     # the model named a known character directly
                st = stretch(t_[1])
                if not (st and st[0] <= p_ <= st[1]):
                    del targets[p_]
        settle_outliers(targets, n)
        for t in {t for t in targets.values() if t[0] == "new"}:
            ps = sorted(p for p, tt in targets.items() if tt == t)
            for p in set(ps) - set(trim_lonely_start(ps, n)):
                del targets[p]
        count = Counter(t for t in targets.values())
        new_ok = {t for t, c in count.items() if t[0] == "new" and c >= MIN_CLUSTER}
        keep = {t: c for t, c in count.items() if t[0] == "known" or t in new_ok}
        if len(keep) < 2:
            checked[bare] = f"{len(mentions)} bare mentions, only one character after the answers"
            continue                                   # only one character after all: nothing to split
        # new entities
        new_ids = {}
        next_id = (cur.execute("SELECT MAX(id) FROM entity").fetchone()[0] or 0) + 1
        owner_ids = plabels.get(bare) or [i for lab in sibs for i in plabels[lab]]
        template = people[max(owner_ids, key=lambda i: len(occ.get(i, [])))]
        for t in sorted(new_ok, key=lambda t: min(p for p, tt in targets.items() if tt == t)):
            ps = sorted(p for p, tt in targets.items() if tt == t)
            lo, hi = ps[len(ps) // 20], ps[-1 - len(ps) // 20]
            label = f"{bare} ({100 * lo // n}-{max(100 * hi // n, 100 * lo // n + 1)}% of the book)"
            first = ps[0]
            cur.execute("INSERT INTO entity (id, label, loc_label, type, count, has_info_card) VALUES (?, ?, ?, 1, 0, ?)",
                        (next_id, label, template["loc"], template["has"]))
            cur.execute("INSERT INTO entity_description (text, source_wildcard, source, entity) VALUES (?, ?, NULL, ?)",
                        (sentence_at(text, first), label, next_id))
            ents[next_id] = {"id": next_id, "label": label, "loc": template["loc"], "type": 1, "count": 0,
                             "has": template["has"], "source": None, "desc": None}
            occ[next_id] = []
            new_ids[t] = next_id
            report["locked"].append(label)
            next_id += 1
        known_ids = {}
        for lab in sibs:
            known_ids[lab] = max(plabels[lab], key=lambda i: len(occ.get(i, [])))
        moved = Counter()
        sizes = Counter()
        for p, t in targets.items():
            if t not in keep:
                continue
            target = new_ids[t] if t[0] == "new" else known_ids[t[1]]
            row = cur.execute("SELECT entity, length FROM occurrence WHERE start = ? AND entity IN "
                              "(SELECT id FROM entity)", (p,)).fetchone()
            if row:
                if row[0] != target:
                    cur.execute("UPDATE occurrence SET entity = ? WHERE start = ? AND entity = ?", (target, p, row[0]))
                    emptied.add(row[0])
                    moved[target] += 1
            else:
                ln = selectable_len(text, p, p + len(bare))
                if not claims_all.overlaps(p, p + ln):
                    cur.execute("INSERT INTO occurrence (entity, start, length) VALUES (?, ?, ?)", (target, p, ln))
                    claims_all.add(p, p + ln)
                    moved[target] += 1
            sizes[target] += 1
        names_ = {t: (t[1] if t[0] == "known" else ents[new_ids[t]]["label"]) for t in keep}
        desc = [f"{names_[t]} {c}" for t, c in sorted(keep.items(), key=lambda kv: -kv[1])]
        report["split"][bare] = {"clusters": chars, "mentions": len(mentions), "assigned": len(targets),
                                 "groups": {names_[t]: c for t, c in keep.items()}}
        log(f"  cast pass: shared name '{bare}' ({len(mentions)} bare mentions, {len(targets)} settled): " + "; ".join(desc))
    report["_emptied"] = sorted(emptied)
