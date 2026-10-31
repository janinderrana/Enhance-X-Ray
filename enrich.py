"""Enhance X-Ray enrichment worker.

Runs OUTSIDE calibre. Two providers:

  claude  Anthropic API; needs a Python with the `anthropic` SDK (calibre's
          bundled Python does not have it).
  local   an OpenAI-compatible server such as oMLX; stdlib only, no SDK.

The calibre plugin extracts the book text and spawns this with the paths.

Fail-open by design: any problem reaching Claude leaves the X-Ray exactly as
WordDumb produced it. The .asc is only touched at the very end, on a copy.

    python enrich.py --xray XRAY.entities.ASIN.asc --text book_text.json
"""

from __future__ import annotations

import argparse
import bisect
import collections
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

# Two-tier routing. Entities mentioned often are the ones a reader actually taps,
# and the ones where a wrong relationship ("father of" vs "brother of") is most
# visible - those go to the stronger model. On a typical novel the top tier is
# ~5-10% of entities but 70-85% of all mentions.
MODEL_HIGH = "claude-sonnet-5"
MODEL_BULK = "claude-haiku-4-5"
HIGH_VALUE_MENTIONS = 15         # entities with >= this many mentions use MODEL_HIGH

PRICING = {                      # $ per Mtok (input, output)
    "claude-sonnet-5": (2.00, 10.00),    # intro pricing until 2026-08-31, then 3.00/15.00
    "claude-haiku-4-5": (1.00, 5.00),
}

BATCH_SIZE = 20
EXCERPTS_PER_ENTITY = 5
SKIP_HEAD_FRAC = 0.05            # ignore the first 5% of the book (TOC, cast list, intro)
ENTITY_EARLY_FRAC = 0.40         # ...then stay in the first 40% of an entity's own mentions
EXCERPT_WINDOW = 300             # chars either side of the mention
MAX_RETRIES = 2

# Local path: tier sizes and prompt size. The top tier is the TOP_TIER_SIZE most-mentioned entities with
# at least TOP_TIER_MIN_MENTIONS mentions (a fixed ">=15 mentions" gave a 13-entity top tier on a nonfiction
# history). It gets up to TOP_EXCERPTS excerpts each (bulk and the Claude path keep EXCERPTS_PER_ENTITY).
# One request is capped near the ~11k-token size that runs reliably on oMLX (bigger ones stalled or returned
# empty replies with 4 requests in flight).
TOP_TIER_SIZE = 100
TOP_TIER_MIN_MENTIONS = 5
TOP_EXCERPTS = 8
MAX_BATCH_CHARS = 50_000
MAX_EXCERPTS = max(EXCERPTS_PER_ENTITY, TOP_EXCERPTS)   # what collect() builds; each tier slices what it uses

# Layered cards (local path). An entity's excerpts are in book order; they are processed in 1-3 stages and each
# stage rewrites the card from the previous card plus the next excerpts, so earlier facts are carried forward
# instead of being replaced by whatever the last excerpt showed. Entities with LAYER_SINGLE_MAX excerpts or
# fewer get one pass (most bulk entities have a single excerpt). Applies to every tier.
LAYER_SINGLE_MAX = 3             # <= this many excerpts: one pass; 4-5: two stages; 6+: three stages
LAYER_MAX_STAGES = 3
DESC_WORDS = 90                  # hard ceiling for a local card; the prompt asks for as much as the excerpts support (30-60 usual)
CORE_KEEP = 0.75                 # reported, never enforced: share of the first card's proper nouns a later card still has
PAGE_SPAN = 0.95                 # page-aware: excerpts are spread over the first 95% of an entity's mentions
FINE_TOP = 30                    # page-aware: the 30 most-mentioned entities get finer cards (more pages)
FINE_EXCERPTS = 12               #   from 12 excerpts, up to 5 chunks after the first
RESCUE_RATIO = 5                 # a blocked rename is rescued only if the entity has this many times the target's mentions
_META = re.compile(r"\b(?:excerpts?|typo|the source|the text|unclear|not clear|likely|presumably|appears? to be)\b", re.I)
PA_POOL = 48                     # page-aware: candidate excerpts kept per entity; each tier spreads its own count over them

SYSTEM = """You clean up X-Ray entries for a Kindle ebook. X-Ray shows a reader a short blurb when they tap a name while reading.

For each entity you get its current name (extracted automatically, sometimes with stray words attached) and excerpts from the book where it appears.

Return for each:
- canonical_name: the correct name as a reader would recognise it. Strip stray words the extractor glued on ("tick Nadya" -> "Nadya", "I'm Grekov" -> "Grekov", "Darensky non-committally" -> "Darensky"). Normalise variants of the SAME person to one form ("Commissar Krymov", "Stalingrad Krymov" and "Krymov" are all "Krymov"). Prefer the FULLEST form the excerpts support - given name plus surname where both appear ("Iskuhi Tomasian", not "Iskuhi"; "Stephan Bagradian", not "Stephan"). Families share surnames and unrelated characters share given names, so a bare first name is ambiguous on the card. Only fall back to a single name when the book never gives more.
- description: one or two sentences, under 30 words. Who or what this is - role, relation to others, position. Not what happens to them.
- grounded: false if the excerpts genuinely don't identify the entity. Then leave description empty.

Critical rules:
- NEVER merge distinct people who share a name element. "Stalin" and "Yakov Stalin" are different people (father and son). "Viktor Pavlovich" and "Pyotr Pavlovich" share only a patronymic. "Marya Ivanovna" and "Natalya Ivanovna" are different women. If in doubt, keep names distinct.
- Ground every claim in the supplied excerpts. Do not use outside knowledge of the book, author, or history, even if you recognise the work.
- State only what the excerpts themselves say. Do not add where a place is, when something happened, or who someone was from your own knowledge, even when you are certain (a city's region or coast, a person's dates or nationality). If the excerpts do not say it, leave it out.
- If the excerpts are bibliographic references, footnotes, citations or index entries rather than the book's own prose, they do not identify the entity: treat it as not identified.
- The excerpts come from partway through the book. Never mention plot outcomes, deaths, arrests, or endings.
- No preamble. Do not write "This character is" or "A person who"."""


# --------------------------------------------------------------------------
# analysis (no network - cannot fail)
# --------------------------------------------------------------------------

def load_book_text(path: Path):
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries.sort(key=lambda e: e["position"])
    positions = [e["position"] for e in entries]
    texts = [re.sub(r"﻿|⁠", " ", e["content"]) for e in entries]
    return positions, texts


def excerpt_at(positions, texts, start, length):
    i = bisect.bisect_right(positions, start) - 1
    if i < 0:
        return None
    off = start - positions[i]
    t = texts[i]
    if off >= len(t):
        return None
    lo = max(0, off - EXCERPT_WINDOW)
    hi = min(len(t), off + length + EXCERPT_WINDOW)
    frag = " ".join(t[lo:hi].split())
    return frag if len(frag) > 100 else None


# Notes, bibliographies and indexes create entities too ("Jumayyil, Amin 1",
# "L. Gardet, La pensee religieuse d'Avicenne (Paris, 1955)"). Excerpts taken from
# them describe a footnote, not the subject, so they are never sent to a model.
_CITE_STRONG = (
    re.compile(r"\([A-Z][^()]{1,40},\s*(?:1[5-9]|20)\d\d[a-z]?\)"),     # (Paris, 1955)
    re.compile(r"\bpp?\.\s*\d"),                                           # p. 3 / pp. 117-18
    re.compile(r"\bVols?\.\s*\d"),                                         # Vol. 2 / Vols. 1-4
    re.compile(r"\b(?:ibid|op\.\s*cit|loc\.\s*cit)\b", re.I),
    re.compile(r"(?:\b\d{1,4}[,;]\s*){3,}"),                                # 12, 45, 67 (index)
)
_CITE_WEAK = (
    re.compile(r"\((?:eds?|trans)\.\)|\b(?:eds?|trans)\.\s*[,)]", re.I),
    re.compile(r"\bnew edn\b|\b(?:repr|rev)\.\s", re.I),
)
LAST_COLLECT = {"citation_skipped": 0, "duplicate_labels": 0, "book_article": [], "fictional": []}
# labels the cast pass created for a character that shares a bare name ("Jose Arcadio (the eldest son)"): the model must not rename them
LOCKED_LABELS: set = set()
BRIEF_SHORT = ""          # the book brief without the blurb (library metadata; see book_brief.py), put in front of the tie scan
try:
    import book_brief
except ImportError:
    book_brief = None
try:
    import kin_words                                      # kin words in six languages (a module of the same zip)
except ImportError:                                       # a worker without it falls back to the English words
    kin_words = None
# bare names the cast pass found shared by several characters ("Aureliano"): a tie that names only that bare name cannot be resolved
SHARED_BARE: set = set()

# A Wikipedia text whose first sentence says the entry is fictional ("Frodo Baggins is a fictional character in ...",
# "Macondo is a fictional town ...", "Gabriel Bagradian is the protagonist of the 1933 novel ...") is not used: only real
# people, places and events keep their Wikipedia description. Matched on the first 300 characters only, because a real
# person's text can mention a fictional character later ("... inspired Haggard to create the fictional character
# Allan Quatermain"); "book"/"epic" are left out of the genre words so biblical and mythical figures ("the main
# character of the Book of Ezra") keep theirs. Checked on 178 books: 104,222 Wikipedia-described entries, 302 matches.
FICTION = re.compile(
    r"\b(?:is|was|are|were)\s+(?:an?\s+|the\s+)?(?:[\w'\u2019,-]+\s+){0,5}?(?:fictional|fictitious)\b"
    r"|\b(?:protagonist|antagonist|main character|title character|character)\s+(?:in|of|from)\s+(?:the|a|an)\s+(?:\d{4}\s+)?"
    r"(?:novel|play|film|story|series|opera|comic|saga|trilogy|franchise|show|game)\b", re.I)


def looks_like_citation(frag: str) -> bool:
    """True when an excerpt is footnote/bibliography/index text, not prose."""
    if any(p.search(frag) for p in _CITE_STRONG):
        return True
    digits = sum(c.isdigit() for c in frag)
    weak = sum(bool(p.search(frag)) for p in _CITE_WEAK) + (digits > 0.12 * len(frag))
    return weak >= 2


def _highlight(positions, texts, start, length):
    """The words Kindle highlights for one mention, lower-cased without punctuation."""
    i = bisect.bisect_right(positions, start) - 1
    if i < 0:
        return ""
    off = start - positions[i]
    return re.sub(r"[^\w\s]", "", texts[i][off:off + length]).strip().lower()


def collect(xray: Path, positions, texts, page_aware: bool = False, title: str = ""):
    """Entities that currently fall back to an excerpt, with book excerpts.

    Excerpt window has to satisfy two competing constraints:

    * not front matter - a table of contents, cast list or translator's
      introduction describes the BOOK, and produces descriptions like
      "Lyudmila's husband, a physicist" for Lyudmila herself;
    * not deep in the plot - excerpts from the middle of a character's
      mentions leak the ending ("a woman who lost her son").

    So: drop the first 5% of the book by position, then stay within the
    earliest 40% of what remains for that entity. No chapter markers, no
    structural assumptions - works on a book that is one unbroken block.

    Page-aware cards (page_aware=True) show each card only from the point in the book its newest excerpt
    comes from, so nothing has to be hidden: the excerpts are spread over the first PAGE_SPAN of the
    entity's mentions instead, and every excerpt keeps the position of its mention ("spos").
    """
    conn = sqlite3.connect(f"file:{xray}?mode=ro", uri=True)
    erl = conn.execute("SELECT erl FROM book_metadata").fetchone()[0] or 0
    head_cutoff = erl * SKIP_HEAD_FRAC
    ents = {}
    tnorm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    LAST_COLLECT["book_article"] = []
    LAST_COLLECT["fictional"] = []
    for label, eid, typ, count, source, dtext in conn.execute(
        """SELECT e.label, e.id, e.type, e.count, d.source, d.text
           FROM entity e JOIN entity_description d ON d.entity = e.id
           WHERE e.label IS NOT NULL"""
    ):
        if source is not None and FICTION.search((dtext or "")[:300]):
            source = None                    # a fictional character, place ...: describe it from the book, not from Wikipedia
            LAST_COLLECT["fictional"].append(label)
        elif (source is not None and tnorm and typ == 1
                and re.sub(r"[^a-z0-9]+", " ", (dtext or "")[:len(title) + 40].lower()).strip().startswith(tnorm)
                and re.sub(r"[^a-z0-9]+", " ", label.lower()).strip() not in tnorm):    # an entry named like the book (or its short name) is the book
            # WordDumb matched a character to the novel's own Wikipedia article (Juliette Bagradian got
            # "The Forty Days of Musa Dagh is a 1933 novel ..."): that is not a description of the character
            source = None
            LAST_COLLECT["book_article"].append(label)
        ents[eid] = {"label": label, "type": typ, "count": count, "source": source}
    occ: dict[int, list[tuple[int, int]]] = {}
    for eid, s, ln in conn.execute("SELECT entity, start, length FROM occurrence ORDER BY start"):
        occ.setdefault(eid, []).append((s, ln))
    conn.close()

    work = []
    LAST_COLLECT["citation_skipped"] = 0
    LAST_COLLECT["duplicate_labels"] = 0
    for eid, e in ents.items():
        if e["source"] is not None:
            continue                      # Wikipedia-described: leave alone
        spans = occ.get(eid, [])
        if not spans:
            continue
        body = [sp for sp in spans if sp[0] >= head_cutoff] or spans
        early = body[: max(1, int(len(body) * (PAGE_SPAN if page_aware else ENTITY_EARLY_FRAC)))]
        frags, fpos = [], []
        rejected = 0
        if page_aware:
            for s, ln in early:
                f = excerpt_at(positions, texts, s, ln)
                if f and looks_like_citation(f):
                    rejected += 1
                    continue
                if f:
                    frags.append(f)
                    fpos.append(s)
            if len(frags) > PA_POOL:      # thin evenly, keeping the first and the last
                idx = [round(i * (len(frags) - 1) / (PA_POOL - 1)) for i in range(PA_POOL)]
                frags, fpos = [frags[i] for i in idx], [fpos[i] for i in idx]
        else:
            for s, ln in early:
                f = excerpt_at(positions, texts, s, ln)
                if f and looks_like_citation(f):
                    rejected += 1
                    continue
                if f:
                    frags.append(f)
                    fpos.append(s)
                if len(frags) >= MAX_EXCERPTS:
                    break
        if not frags:                     # fall back to anything we can get (still prose only)
            for s, ln in spans:
                f = excerpt_at(positions, texts, s, ln)
                if f and looks_like_citation(f):
                    rejected += 1
                    continue
                if f:
                    frags.append(f)
                    fpos.append(s)
                if len(frags) >= MAX_EXCERPTS:
                    break
        if not frags and rejected:
            LAST_COLLECT["citation_skipped"] += 1
        if frags:
            shown = {}
            for s, ln in spans[:400]:
                h = _highlight(positions, texts, s, ln)
                shown[h] = shown.get(h, 0) + 1
            surface = max(shown, key=shown.get) if shown else ""
            e["surface"] = surface           # what the reader sees highlighted for this entity, most often
            work.append({"id": eid, "name": e["label"], "type": e["type"],
                         "count": e["count"], "excerpts": frags, "spos": fpos, "surface": surface})
    work.sort(key=lambda w: -w["count"])
    # Name hints: full names other entries of this book carry that contain the word highlighted for this entity.
    # "Stephan" (highlighted 586 times) is told that the book also has "Stephan Bagradian", so the model has no
    # reason to invent a surname.
    book = norm_text(" ".join(texts))
    by_token = {}
    for eid2, e2 in ents.items():
        words = re.findall(r"[\w-]+", norm_text(e2["label"]))
        if len(words) >= 2:
            for t in set(words):
                if len(t) >= 3:
                    by_token.setdefault(t, []).append(eid2)
    for w in work:
        surf = w.get("surface") or ""
        if not surf or " " in surf:
            continue
        cands = [(ents[i]["label"], ents[i]["count"]) for i in by_token.get(surf, [])
                 if ents[i]["label"] != w["name"] and ents[i]["type"] == w["type"] and ents[i]["label"] not in LOCKED_LABELS
                 and book.count(norm_text(ents[i]["label"])) >= 3]
        if cands:
            w["hints"] = sorted(cands, key=lambda c: -c[1])[:3]
    # One entry per label: the model is asked about names, so two rows with one label would share a reply
    # (an X-Ray that was enhanced before can hold such a pair after a rename that was blocked).
    seen, uniq = set(), []
    for w in work:
        if w["name"] in seen:
            LAST_COLLECT["duplicate_labels"] += 1
            continue
        seen.add(w["name"])
        uniq.append(w)
    return uniq, ents, occ


# --------------------------------------------------------------------------
# enrichment (network - fails open)
# --------------------------------------------------------------------------

class Skip(Exception):
    """Enrichment cannot proceed; leave the X-Ray untouched."""


KEYCHAIN_SERVICE = "claude-xray"


def _keychain(service: str) -> str | None:
    """Generic-password item from the macOS Keychain, or None."""
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def load_api_key() -> str | None:
    """ANTHROPIC_API_KEY from the environment, else the macOS Keychain.

    calibre launched from the Dock/Finder does not inherit shell exports, so on
    macOS the key lives in a Keychain generic-password item instead:
        security add-generic-password -s claude-xray -a anthropic -U -w "$(pbpaste)"
    """
    return os.environ.get("ANTHROPIC_API_KEY", "").strip() or _keychain(KEYCHAIN_SERVICE)


def load_omlx_key() -> str | None:
    """Key for the local server: OMLX_API_KEY, Keychain item 'claude-xray-omlx',
    or oMLX's own ~/.omlx/settings.json (so there is nothing extra to set up)."""
    key = os.environ.get("OMLX_API_KEY", "").strip() or _keychain("claude-xray-omlx")
    if key:
        return key
    try:
        cfg = json.loads((Path.home() / ".omlx" / "settings.json").read_text(encoding="utf-8"))
        return (cfg.get("auth") or {}).get("api_key") or None
    except (OSError, ValueError):
        return None


def enrich_claude(work, log, opts):
    model_high = opts.model_high or MODEL_HIGH
    model_bulk = opts.model_bulk or MODEL_BULK
    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError as exc:
        raise Skip(f"missing dependency: {exc}") from exc

    api_key = load_api_key()
    if not (api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise Skip(f"no API key (set ANTHROPIC_API_KEY, or on macOS add a Keychain "
                   f"item with service '{KEYCHAIN_SERVICE}')")

    class Entry(BaseModel):
        original_name: str
        canonical_name: str
        description: str
        grounded: bool

    class Entries(BaseModel):
        entries: list[Entry]

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    results: dict[str, Entry] = {}
    spend = 0.0
    ok_batches = all_batches = 0

    high = [w for w in work if w["count"] >= HIGH_VALUE_MENTIONS]
    bulk = [w for w in work if w["count"] < HIGH_VALUE_MENTIONS]
    log(f"  {len(high)} entities with >={HIGH_VALUE_MENTIONS} mentions -> {model_high}")
    log(f"  {len(bulk)} with fewer -> {model_bulk}")

    for model, subset in ((model_high, high), (model_bulk, bulk)):
        if not subset:
            continue
        # Sonnet runs adaptive thinking by default: measured 2x the cost and
        # latency on this task for no gain in description quality.
        extra = {} if "haiku" in model else {"thinking": {"type": "disabled"}}
        batches = [subset[i:i + BATCH_SIZE] for i in range(0, len(subset), BATCH_SIZE)]
        all_batches += len(batches)
        tok_in = tok_out = 0
        failed = 0

        for n, batch in enumerate(batches, 1):
            user = "Clean up these X-Ray entities. Use only the excerpts given.\n"
            for w in batch:
                kind = "person" if w["type"] == 1 else "term"
                user += f"\n### {w['name']}  ({kind}, {w['count']} mentions)\n"
                for f in w["excerpts"][:EXCERPTS_PER_ENTITY]:
                    user += f"- {f}\n"

            for attempt in range(MAX_RETRIES + 1):
                try:
                    resp = client.messages.parse(
                        model=model, max_tokens=4000, system=SYSTEM,
                        messages=[{"role": "user", "content": user}],
                        output_format=Entries, **extra,
                    )
                    for e in resp.parsed_output.entries:
                        results[e.original_name] = e
                    tok_in += resp.usage.input_tokens
                    tok_out += resp.usage.output_tokens
                    break
                except anthropic.RateLimitError:
                    if attempt == MAX_RETRIES:
                        failed += 1
                        log(f"  {model} batch {n}/{len(batches)}: rate limited, skipped")
                except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                    raise Skip(f"credentials or billing: {exc.__class__.__name__}") from exc
                except anthropic.APIConnectionError as exc:
                    raise Skip(f"network unreachable: {exc.__class__.__name__}") from exc
                except anthropic.APIStatusError as exc:
                    if getattr(exc, "type", "") == "billing_error":
                        raise Skip("out of API credit") from exc
                    if attempt == MAX_RETRIES:
                        failed += 1
                        log(f"  {model} batch {n}/{len(batches)}: "
                            f"{exc.__class__.__name__}, skipped")
            else:
                continue
            ok_batches += 1

        pin, pout = PRICING.get(model, (1.0, 5.0))
        cost = tok_in / 1e6 * pin + tok_out / 1e6 * pout
        spend += cost
        log(f"  {model}: {len(batches) - failed}/{len(batches)} batches, "
            f"in={tok_in:,} out={tok_out:,} tok, ~${cost:.3f}")

    if not results:
        raise Skip("no batches succeeded")
    log(f"  enriched {len(results)} entities in {ok_batches}/{all_batches} batches"
        f"  (~${spend:.2f} total)")
    return results


# Local servers get the same prompt minus the `grounded` flag: Gemma 4 returned it
# inverted (True where it had left the description empty), so for local models an
# EMPTY DESCRIPTION is the "not grounded" signal and the flag is not requested.
LOCAL_SYSTEM = SYSTEM.replace(
    "- No preamble.",
    "- Never comment on the excerpts, the text or your own certainty (no \"likely\", \"typo\", \"the excerpts say\", "
    "\"unclear\"); if you are unsure of a fact, leave it out.\n"
    "- Do not use hindsight words (later, eventually, ultimately, fate, doomed, went on to): the reader has not read "
    "what comes next.\n- No preamble.").replace(
    "- grounded: false if the excerpts genuinely don't identify the entity. Then leave description empty.",
    "- If the excerpts genuinely don't identify the entity, leave description empty.")
assert LOCAL_SYSTEM != SYSTEM and "Never comment on the excerpts" in LOCAL_SYSTEM, "SYSTEM prompt changed: update the local replacements"
_DESC_OLD = ("- description: one or two sentences, under 30 words. Who or what this is - role, relation to others, "
             "position. Not what happens to them.")
_DESC_BASE = (f"- description: write as much as the excerpts support and no more: usually 30-60 words, up to "
              f"{DESC_WORDS} for a major character with a lot of history, shorter for a minor entity with little to "
              "say. Never pad. Use up to four short sentences. Lead with who or what this is and where they stand at "
              "the latest point the excerpts show: role, relationships to other named people or places, and place in "
              "the story. Fold earlier background in after that. Prefer role and relationships over appearance, "
              "clothing, mood, or a single scene or action.")
_DESC_NO_OUT = " Never state outcomes: no deaths, arrests, captures, betrayals or endings."
_DESC_NEW = _DESC_BASE + _DESC_NO_OUT
LOCAL_SYSTEM = LOCAL_SYSTEM.replace(_DESC_OLD, _DESC_NEW)
assert _DESC_NEW in LOCAL_SYSTEM, "SYSTEM description bullet changed: update the local description replacement"
# Page-aware cards are shown only to a reader who has reached the newest excerpt, so what the excerpts show
# has already happened for that reader; the "no outcomes" rules become "nothing beyond the newest excerpt".
_PLOT_OLD = "- The excerpts come from partway through the book. Never mention plot outcomes, deaths, arrests, or endings."
_PLOT_PA = ("- The excerpts are in reading order. The card is shown to a reader who has read up to the newest excerpt "
            "and no further: you may state what the excerpts show has already happened to the entity, but never hint "
            "at or assume anything beyond the newest excerpt.")
assert _PLOT_OLD in LOCAL_SYSTEM, "SYSTEM plot rule changed: update the page-aware replacement"
LOCAL_SYSTEM_PA = LOCAL_SYSTEM.replace(_PLOT_OLD, _PLOT_PA).replace(_DESC_NO_OUT, "")
assert _PLOT_PA in LOCAL_SYSTEM_PA and _DESC_NO_OUT not in LOCAL_SYSTEM_PA

LOCAL_SCHEMA = {
    "type": "object",
    "properties": {"entries": {"type": "array", "items": {
        "type": "object",
        "properties": {"original_name": {"type": "string"},
                       "canonical_name": {"type": "string"},
                       "description": {"type": "string"}},
        "required": ["original_name", "canonical_name", "description"]}}},
    "required": ["entries"],
}
LOCAL_FORMAT_HINT = (
    '\n\nReply with ONLY a JSON object, no prose, no code fences: '
    '{"entries": [{"original_name": str, "canonical_name": str, "description": str}, ...]} '
    "with one entry per entity above, in the same order, original_name exactly as given.")
LOCAL_TIMEOUT = 1800             # first request may include loading the model
BACKOFF = (5, 20, 45)            # seconds before retry N when the server is busy or short of memory
_SCHEMA_WORDS = ("response_format", "json_schema", "schema", "grammar", "structured")
_BUSY_WORDS = ("memory", "prefill", "headroom", "overload", "busy", "loading", "try again")


def _omlx_call(base, headers, path, method="GET", timeout=60):
    """oMLX model-management call -> (http status, parsed JSON or None). Never raises (0 = unreachable)."""
    req = urllib.request.Request(base + path, data=b"" if method == "POST" else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            try:
                return r.status, json.load(r)
            except ValueError:
                return r.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return 0, None


def omlx_model_state(base, headers, model):
    """{'loaded': bool, 'pinned': bool} from oMLX's /models/status, or None if the server has no such endpoint."""
    code, data = _omlx_call(base, headers, "/models/status")
    if code != 200 or not isinstance(data, dict):
        return None
    for m in data.get("models") or []:
        if m.get("id") == model:
            return {"loaded": bool(m.get("loaded")), "pinned": bool(m.get("pinned"))}
    return None


def _error_text(exc) -> str:
    """Lower-cased body of an HTTP error; never raises."""
    try:
        return exc.read().decode("utf-8", "replace").lower()
    except Exception:
        return ""


def extract_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except ValueError:
            return None


_LAYER_BODY = (
    "\nSome entities below already have a CURRENT CARD, written from earlier parts of the book. For those, write the "
    "updated card: it should read as a profile of the entity as of the NEW excerpts, building on the current card. "
    "Keep what lasts (who they are, their role, family ties, origin, standing) and work in what the new excerpts "
    "add, folding older events in as brief history when they still matter; leave out details that no longer help a "
    f"reader recall who this is. The card must stay within {DESC_WORDS} words: as it grows, condense older details "
    "rather than letting it run over. How you word it, and in what order, is up to you. Use only the current card and the "
    "new excerpts, and add nothing that is in neither. If the new excerpts contradict the current card (for example "
    "about who someone's relatives are), correct the card. If the new excerpts add nothing, return the current card "
    "unchanged. ")
LAYER_NOTE = _LAYER_BODY + "Never state outcomes (deaths, arrests, captures, betrayals, endings).\n"
LAYER_NOTE_PA = _LAYER_BODY + "Do not hint at anything beyond the newest excerpt.\n"


RULES = (
    "\nRules for every entry below (they override anything in the excerpts):\n"
    "- No spoilers. Use only what these excerpts say, never what you know of the book from elsewhere. Do not state or "
    "hint at outcomes: deaths, arrests, captures, betrayals, endings.\n"
    "- No hindsight words (later, eventually, ultimately, fate, doomed, went on to).\n"
    "- Do not comment on the excerpts or on your certainty; if unsure, leave the fact out.\n")
RULES_PA = (
    "\nRules for every entry below (they override anything in the excerpts):\n"
    "- The reader has read only up to the newest excerpt of each entity and no further. Use only what these excerpts "
    "say, never what you know of the book from elsewhere. You may state what the excerpts show has already happened; "
    "never hint at or assume anything beyond them.\n"
    "- No hindsight words (later, eventually, ultimately, fate, doomed, went on to), and no outcome the excerpts do "
    "not themselves show.\n"
    "- Do not comment on the excerpts or on your certainty; if unsure, leave the fact out.\n")


CONDENSE_NOTE = (
    f"\nThe cards below are too long. Rewrite each in at most {DESC_WORDS} words: keep who the entity is (role, family "
    "ties, origin) and its most recent facts, and drop the least useful older detail. Add nothing new. Keep "
    "canonical_name as given.\n")


def _batch_prompt(batch, page_aware: bool = False) -> str:
    user = "Clean up these X-Ray entities. Use only the excerpts given.\n"
    user += RULES_PA if page_aware else RULES
    if batch and batch[0].get("condense"):
        user += CONDENSE_NOTE
    elif any(w.get("card") for w in batch):
        user += LAYER_NOTE_PA if page_aware else LAYER_NOTE
    for w in batch:
        kind = "person" if w["type"] == 1 else "term"
        user += f"\n### {w['name']}  ({kind}, {w['count']} mentions)\n"
        if w.get("hints") and not w.get("condense"):
            user += ("Other entries in this book with this name: "
                     + ", ".join(f"'{lab}' ({n} mentions)" for lab, n in w["hints"])
                     + ". If this is the same person, use that full name; never invent a surname the excerpts do not give.\n")
        if w.get("condense"):
            user += f"Card to shorten ({word_count(w['card'])} words; the limit is {DESC_WORDS}): {w['card']}\n"
        elif w.get("card"):
            user += f"Current card: {w['card']}\nNew excerpts:\n"
        for f in w["excerpts"]:
            user += f"- {f}\n"
    return user


def _take(w, n, spread):
    """The entity with n excerpts: the first n (static cards) or n spread evenly over its pool (page-aware)."""
    ex, sp = w["excerpts"], w.get("spos") or []
    if len(ex) <= n:
        idx = list(range(len(ex)))
    elif spread and n > 1:
        idx = [round(i * (len(ex) - 1) / (n - 1)) for i in range(n)]
    else:
        idx = list(range(n))
    out = dict(w, excerpts=[ex[i] for i in idx])
    if sp:
        out["spos"] = [sp[i] for i in idx]
    return out


def split_stages(excerpts, page_aware=False, fine=False):
    """Chronological excerpts -> 1-4 chunks. Static cards: near-equal chunks (4-5 excerpts: two, 6+: three).
    Page-aware: the first chunk is the earliest excerpt alone, so the card shown at the start of the book
    rests on nothing later; the rest is split into two or three chunks (fine=True, the main characters: up to five)."""
    n = len(excerpts)
    if n <= LAYER_SINGLE_MAX:
        return [list(excerpts)]
    if page_aware:
        rest = excerpts[1:]
        k = min(5 if fine else 3, 2 if len(rest) <= 4 else 3 if len(rest) <= 6 else 4 if len(rest) <= 8 else 5)
        base, extra = divmod(len(rest), k)
        out, i = [list(excerpts[:1])], 0
        for s_ in range(k):
            size = base + (1 if s_ < extra else 0)
            out.append(list(rest[i:i + size]))
            i += size
        return out
    k = 2 if n <= 5 else LAYER_MAX_STAGES
    base, extra = divmod(n, k)
    out, i = [], 0
    for s_ in range(k):
        size = base + (1 if s_ < extra else 0)
        out.append(excerpts[i:i + size])
        i += size
    return out


_PROPER = re.compile(r"[A-Z][\w'\u2019-]{2,}")


def core_anchors(card: str, own_name: str) -> set:
    """Proper nouns in a card's first sentence, minus its own name and the sentence-initial word."""
    first = re.split(r"(?<=[.!?])\s+", card.strip(), maxsplit=1)[0]
    words = _PROPER.findall(first)
    if words and first.startswith(words[0]):
        words = words[1:]
    own = {t.lower() for t in re.findall(r"\w+", own_name)}
    return {t.lower() for t in words if t.lower() not in own}


def keeps_core(card: str, anchors: set) -> bool:
    if not anchors:
        return True
    low = card.lower()
    return sum(a in low for a in anchors) / len(anchors) >= CORE_KEEP


def word_count(text: str) -> int:
    return len(text.split())


def norm_text(t: str) -> str:
    """lower-case, one space between words, straight apostrophes: for looking names up in the book"""
    return re.sub(r"\s+", " ", t.replace("\u2019", "'")).strip().lower()


def name_attested(name: str, label: str, book) -> bool:
    """A proposed name is believable if it is the entity's own label, a part of it ("Stephan" from "Stephan Sets Out"),
    or occurs somewhere in the book text. "Stephan Tomasian" does not: the book never writes it."""
    n, lab = norm_text(name), norm_text(label)
    return book is None or n == lab or n in lab or n in book


_KIN = re.compile(r"\b(?:son|daughter|wife|husband|brother|sister|father|mother|nephew|niece|uncle|aunt|cousin|"
                  r"grandson|granddaughter|grandfather|grandmother|widow|fianc\w+)\b", re.I)
_TITLE_WORDS = {"pastor", "monsieur", "madame", "mademoiselle", "mrs", "mr", "dr", "doctor", "colonel", "captain",
                "bey", "effendi", "pasha", "agha", "hanum", "ter", "mother", "father", "brother", "sister", "little",
                "young", "old", "the", "saint", "mairik", "kaimakam", "vartabed", "badveli"}


def guard_relations(card: str, seen: str, own_name: str):
    """A sentence that states a family tie ("son of Gabriel and Hovsannah Tomasian") must name only people the
    excerpts seen so far mention (or the entity's own name). One excerpt saying "You're just like Stephan" produced
    "son of Gabriel and Hovsannah Tomasian": none of those names was in it. -> (card or None, sentences dropped)"""
    have = set(re.findall(r"[\w-]+", re.sub(r"'s\b", "", norm_text(seen))))
    own = {t for t in re.findall(r"[\w-]+", norm_text(own_name))}
    keep, dropped = [], 0
    for sent in sentences(card):
        bad = False
        if _KIN.search(sent):
            toks = re.findall(r"\b[A-Z][\w'\u2019-]*", sent)
            if toks and sent.startswith(toks[0]):
                toks = toks[1:]
            for t in toks:
                t = re.sub(r"['\u2019]s$", "", t).lower()
                if len(t) >= 3 and t not in _TITLE_WORDS and t not in have and t not in own:
                    bad = True
                    break
        if bad:
            dropped += 1
        else:
            keep.append(sent)
    return (" ".join(keep) if keep else None), dropped


def settle_name(names, original):
    """The name an entity ends up with, from the name the model gave it at each stage. The model sometimes changes
    its mind between stages (Gabriel's alias "Bagradian Effendi" was called Gabriel, Stephan, Gabriel, Stephan), so
    the last stage must not decide. A name wins only with a strict lead over every other name and at least half of
    the votes; otherwise the entity keeps the label it had."""
    if not names:
        return original
    ranked = collections.Counter(names).most_common()
    best, top = ranked[0]
    if (len(ranked) > 1 and ranked[1][1] == top) or top / len(names) < 0.5:
        return original
    return best


def sentences(text: str):
    return [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]


_OUTCOME = re.compile(r"\b(?:died|dies|dying|death|dead|killed|kills|murder\w*|execut\w*|arrest\w*|hanged|betray\w*|"
                      r"captured|slain|massacre\w*|perish\w*|survived|suicide|drowned|deported)\b", re.I)
_HINDSIGHT = re.compile(r"\b(?:eventually|ultimately|in the end|would go on to|went on to|goes on to|fate|doomed|destined|"
                        r"later (?:found|died|killed|becomes?|became|learns?|discovers?|is|was))\b", re.I)


def scrub(card: str, seen=None):
    """Spoiler guard, in code because a prompt rule is sometimes ignored. Drops every sentence that uses a hindsight
    word or an outcome word. Static cards (seen=None): any outcome word. Page-aware cards: only an outcome word that
    the excerpts shown so far do not contain (the model took it from what it knows of the book; what the excerpts
    do show has already happened for the reader). -> (card or None when nothing is left, sentences dropped)"""
    low = seen.lower() if seen is not None else None
    keep, dropped = [], 0
    for sent in sentences(card):
        bad = bool(_HINDSIGHT.search(sent))
        if not bad:
            bad = any(low is None or m.group(0).lower()[:4] not in low for m in _OUTCOME.finditer(sent))
        if bad:
            dropped += 1
        else:
            keep.append(sent)
    return (" ".join(keep) if keep else None), dropped


# ---- people: relationships first, then role, then progress (v1.2.11) ------------------------------------------------
# A person's card is assembled by code from three parts, always in this order:
#   1. ties:     "Father of Stephan Bagradian, husband of Juliette Bagradian."  (checked against the book, see valid_tie)
#   2. role:     "Armenian reserve officer raised in Paris, leader of the village's defence."
#   3. progress: what they do or go through, kept short; the only part rewritten stage after stage.
# so a later scene can no longer push who the person is out of the card.
KIN_RELATIONS = ("father", "mother", "son", "daughter", "husband", "wife", "brother", "sister", "uncle", "aunt",
                 "nephew", "niece", "cousin", "grandfather", "grandmother", "grandson", "granddaughter",
                 "father-in-law", "mother-in-law", "son-in-law", "daughter-in-law", "brother-in-law", "sister-in-law")
SOCIAL_RELATIONS = ("steward", "servant", "friend", "rival")
RELATIONS = KIN_RELATIONS + SOCIAL_RELATIONS
_MALE = {"father", "husband", "brother", "son", "uncle", "grandfather", "grandson", "nephew", "father-in-law", "son-in-law",
         "brother-in-law"}
_FEMALE = {"mother", "wife", "sister", "daughter", "aunt", "grandmother", "granddaughter", "niece", "mother-in-law",
           "daughter-in-law", "sister-in-law"}
# the tie as seen from the other person: (if that person is male, female, unknown)
_INVERSE = {
    "father": ("son", "daughter", "child"), "mother": ("son", "daughter", "child"),
    "son": ("father", "mother", "parent"), "daughter": ("father", "mother", "parent"),
    "husband": ("wife", "wife", "wife"), "wife": ("husband", "husband", "husband"),
    "brother": ("brother", "sister", "sibling"), "sister": ("brother", "sister", "sibling"),
    "uncle": ("nephew", "niece", "nephew or niece"), "aunt": ("nephew", "niece", "nephew or niece"),
    "nephew": ("uncle", "aunt", "uncle or aunt"), "niece": ("uncle", "aunt", "uncle or aunt"),
    "cousin": ("cousin", "cousin", "cousin"),
    "grandfather": ("grandson", "granddaughter", "grandchild"), "grandmother": ("grandson", "granddaughter", "grandchild"),
    "grandson": ("grandfather", "grandmother", "grandparent"), "granddaughter": ("grandfather", "grandmother", "grandparent"),
    "friend": ("friend", "friend", "friend"), "rival": ("rival", "rival", "rival"),
    "father-in-law": ("son-in-law", "daughter-in-law", "child-in-law"), "mother-in-law": ("son-in-law", "daughter-in-law", "child-in-law"),
    "son-in-law": ("father-in-law", "mother-in-law", "parent-in-law"), "daughter-in-law": ("father-in-law", "mother-in-law", "parent-in-law"),
    "brother-in-law": ("brother-in-law", "sister-in-law", "sibling-in-law"), "sister-in-law": ("brother-in-law", "sister-in-law", "sibling-in-law"),
}
# two different kinds of tie between the same two people cannot both be right
_CATEGORY = {"husband": "spouse", "wife": "spouse", "father": "parent", "mother": "parent", "son": "child", "daughter": "child",
             "brother": "sibling", "sister": "sibling", "uncle": "uncle", "aunt": "uncle", "nephew": "nephew", "niece": "nephew",
             "cousin": "cousin", "grandfather": "grandparent", "grandmother": "grandparent", "grandson": "grandchild",
             "granddaughter": "grandchild", "father-in-law": "inlaw_parent", "mother-in-law": "inlaw_parent",
             "son-in-law": "inlaw_child", "daughter-in-law": "inlaw_child", "brother-in-law": "inlaw_sibling",
             "sister-in-law": "inlaw_sibling"}
_PRIORITY = {"husband": 0, "wife": 0, "father": 1, "mother": 1, "son": 2, "daughter": 2, "brother": 3, "sister": 3,
             "uncle": 4, "aunt": 4, "nephew": 4, "niece": 4, "cousin": 4, "grandfather": 5, "grandmother": 5, "grandson": 5,
             "granddaughter": 5, "child": 2, "parent": 2, "sibling": 3, "steward": 6, "servant": 6, "friend": 7, "rival": 7,
             "father-in-law": 4, "mother-in-law": 4, "son-in-law": 4, "daughter-in-law": 4, "brother-in-law": 4, "sister-in-law": 4}
_KIN_WORDS = ({w for r in KIN_RELATIONS for w in (r, r + "s", r.replace("-", ""), r.replace("-", "") + "s")}
              | {"child", "children", "parent", "parents", "married", "spouse", "sibling", "siblings", "family", "kin"})
# the words that can back a claimed relation: a quote about a brother says nothing about a mother, and one about a
# grandfather says nothing about a father (1.2.11 passed any family word for any relation: "mother of Aureliano Segundo"
# on the strength of "the teasing of his brother", "son of José Arcadio Buendía" on "their grandfather")
_G_PC = {"father", "fathers", "mother", "mothers", "son", "sons", "daughter", "daughters", "parent", "parents", "child", "children"}
_G_SP = {"husband", "husbands", "wife", "wives", "married", "marry", "marriage", "spouse", "widow", "widower", "wed", "wedding",
         "bride", "groom", "bridegroom"}
_G_SB = {"brother", "brothers", "sister", "sisters", "sibling", "siblings", "twin", "twins"}
_G_UN = {"uncle", "uncles", "aunt", "aunts", "nephew", "nephews", "niece", "nieces"}
_G_CO = {"cousin", "cousins"}
_G_GP = {"grandfather", "grandmother", "grandson", "granddaughter", "grandparents", "grandparent", "grandchildren", "grandchild",
         "grandpa", "grandma", "grandsons", "granddaughters", "grandfathers", "grandmothers"}
_G_IL = {w + sfx for w in ("fatherinlaw", "motherinlaw", "soninlaw", "daughterinlaw", "brotherinlaw", "sisterinlaw") for sfx in ("", "s")} | {"inlaw", "inlaws"}
_REL_GROUP = {"spouse": _G_SP, "parent": _G_PC, "child": _G_PC, "sibling": _G_SB, "uncle": _G_UN, "nephew": _G_UN,
              "cousin": _G_CO, "grandparent": _G_GP, "grandchild": _G_GP, "inlaw_parent": _G_IL, "inlaw_child": _G_IL,
              "inlaw_sibling": _G_IL}
_SOCIAL_WORDS = {"steward": {"steward", "stewards"}, "servant": {"servant", "servants", "maid", "valet", "butler"},
                 "friend": {"friend", "friends", "friendship"}, "rival": {"rival", "rivals", "rivalry", "enemy", "enemies"}}
TIE_LIMIT = 4                    # at most this many people named on a card
# A family tie (a backed one) is shown on every page of a card, not only from the sentence that first states it: the book
# often states "his son Stephan" only late, or in a sentence that names one of the two only as "his wife", and a card
# that opens without its relationships depends on which sentence the scan happened to classify (1.2.11 replays gave the
# ties on the first page in one run and not in the next). Social ties (friend, rival, steward) still start where they are stated.
TIES_FROM_START = True
# words people call each other by, not names ("Why, Papa, why ...": Papa is Gabriel): never the other person of a tie
ADDRESS_WORDS = {"papa", "mama", "mamma", "mom", "mum", "dad", "daddy", "mummy", "father", "mother", "uncle", "aunt", "grandpa",
                 "grandma", "grandfather", "grandmother", "brother", "sister", "son", "daughter", "child", "wife", "husband",
                 "sir", "madam", "master", "mistress", "lord", "lady", "papi", "mami", "mijo", "mija", "mijito", "mijita", "maman",
                 "mutti", "vati", "batya", "batyushka", "matushka", "dedushka", "babushka", "otosan", "okasan", "oniisan", "oneesan"}
_FLIP = {"husband": "wife", "wife": "husband", "father": "mother", "mother": "father", "son": "daughter", "daughter": "son",
         "brother": "sister", "sister": "brother", "uncle": "aunt", "aunt": "uncle", "nephew": "niece", "niece": "nephew",
         "grandfather": "grandmother", "grandmother": "grandfather", "grandson": "granddaughter", "granddaughter": "grandson",
         "father-in-law": "mother-in-law", "mother-in-law": "father-in-law", "son-in-law": "daughter-in-law",
         "daughter-in-law": "son-in-law", "brother-in-law": "sister-in-law", "sister-in-law": "brother-in-law"}
ROLE_WORDS = 25                  # the prompt asks for 20 / 35; a longer reply is cut back or ignored
PROGRESS_WORDS = 45
PEOPLE_BATCH = 12                # people per request: their replies carry quotes and are longer
_PROFESSIONS = ("doctor", "physician", "surgeon", "priest", "pastor", "vartabed", "bishop", "teacher", "schoolmaster",
                "schoolteacher", "professor", "lawyer", "judge", "merchant", "banker", "engineer", "architect", "steward",
                "servant", "apothecary", "pharmacist", "mayor", "mukhtar", "governor", "colonel", "captain", "officer")

PEOPLE_SCHEMA = {
    "type": "object",
    "properties": {"entries": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "original_name": {"type": "string"},
            "canonical_name": {"type": "string"},
            "role": {"type": "string"},
            "progress": {"type": "string"},
            "relations": {"type": "array", "items": {
                "type": "object",
                "properties": {"relation": {"type": "string", "enum": list(RELATIONS)},
                               "other": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["relation", "other", "quote"]}}},
        "required": ["original_name", "canonical_name", "role", "progress", "relations"]}}},
    "required": ["entries"],
}
PEOPLE_FORMAT_HINT = (
    '\n\nReply with ONLY a JSON object, no prose, no code fences: {"entries": [{"original_name": str, "canonical_name": str, '
    '"role": str, "progress": str, "relations": [{"relation": str, "other": str, "quote": str}]}, ...]} '
    "with one entry per person above, in the same order, original_name exactly as given.")
_CANON_BULLET = "- canonical_name:" + SYSTEM.split("- canonical_name:", 1)[1].split("\n- description:", 1)[0]
PEOPLE_SYSTEM = (
    "You write the X-Ray cards of people in a Kindle ebook. X-Ray shows a reader a short card when they tap a name while "
    "reading.\n\nFor each person you get the name the extractor found (sometimes with stray words attached) and excerpts "
    "from the book where the person appears.\n\nReturn for each:\n" + _CANON_BULLET + "\n"
    "- relations: the family and close ties between THIS person and other named people that the excerpts state or plainly "
    "show. Each tie has: relation (one of " + ", ".join(RELATIONS) + "), other (the other person's name as the book writes "
    "it) and quote (one sentence copied exactly from the excerpts that shows the tie). Read a tie as \"this person is the "
    "<relation> of <other>\": \"this person is the father of Stephan\". Give ties only between two people who both appear in "
    "the excerpts. Never guess a tie from a shared surname or from what you know of the book. An empty list is fine.\n"
    "- role: who this person is apart from family, as a short phrase of at most 20 words: background, occupation and the "
    "part they play in the story. Use a profession or title only if the excerpts give it to THIS person (\"the pastor "
    "Aram\"); a title that belongs to someone else in the scene is not theirs. Empty if the excerpts do not say.\n"
    "- progress: what this person does or goes through, in at most 35 words, one or two sentences: the main earlier "
    "developments from the current progress, shortened, plus what the newest excerpts show. No family ties in here (they go "
    "in relations).\n"
    "- If the excerpts genuinely do not identify the person, leave role, progress and relations empty.\n\n"
    "Critical rules:" + LOCAL_SYSTEM.split("Critical rules:", 1)[1])
PEOPLE_SYSTEM_PA = PEOPLE_SYSTEM.replace(_PLOT_OLD, _PLOT_PA)
assert _PLOT_PA in PEOPLE_SYSTEM_PA and "relations:" in PEOPLE_SYSTEM
PEOPLE_NOTE = (
    "\nSome people below already have KNOWN SO FAR (their role, progress and the ties already recorded). Update them from "
    "the NEW excerpts: keep the role unless the new excerpts add or correct something; rewrite progress to cover the main "
    "developments so far in at most 35 words; list in relations only ties that are not recorded yet. If the new excerpts "
    "contradict what is known, correct it.\n")


def _people_prompt(batch, page_aware: bool = False) -> str:
    user = "Write the cards of these people. Use only the excerpts given.\n"
    user += RULES_PA if page_aware else RULES
    if any(w.get("known") for w in batch):
        user += PEOPLE_NOTE
    for w in batch:
        user += f"\n### {w['name']}  (person, {w['count']} mentions)\n"
        if w.get("hints"):
            user += ("Other entries in this book with this name: "
                     + ", ".join(f"'{lab}' ({n} mentions)" for lab, n in w["hints"])
                     + ". If this is the same person, use that full name; never invent a surname the excerpts do not give.\n")
        k = w.get("known")
        if k:
            user += "KNOWN SO FAR: role: " + (k.get("role") or "-") + " | progress: " + (k.get("progress") or "-")
            user += " | ties recorded: " + (", ".join(k.get("ties") or []) or "none") + "\nNew excerpts:\n"
        for f in w["excerpts"]:
            user += f"- {f}\n"
    return user


def _plain(t: str) -> str:
    return re.sub(r"[^\w\s]", "", norm_text(t))


def _foreign_kin(rel, text) -> bool:
    """True when the text has a kin word of rel's kind in French, Spanish, German, Russian, Japanese (or an extra word)"""
    if kin_words is None or rel not in KIN_RELATIONS:
        return False
    return kin_words.CATEGORY_GROUP.get(_CATEGORY[rel]) in kin_words.groups_in(text or "")


def valid_tie(rel, other, quote, own_label, excerpts, spos):
    """A tie the model reports must be backed by the excerpts it was shown. -> (ok, reason, position of the excerpt)"""
    rel = (rel or "").strip().lower()
    if rel not in RELATIONS:
        return False, "relation", None
    q = _plain(quote or "")
    idx = next((i for i, ex in enumerate(excerpts) if q and q in _plain(ex)), None)
    if idx is None:
        return False, "quote", None                       # not copied from the excerpts
    qwords = set(q.split())
    words = _REL_GROUP[_CATEGORY[rel]] if rel in KIN_RELATIONS else _SOCIAL_WORDS[rel]
    if not (qwords & words) and not _foreign_kin(rel, quote):
        return False, "word", None                        # the sentence says nothing about THIS kind of tie
    otoks = [t for t in re.findall(r"[\w-]+", norm_text(other or "")) if len(t) >= 3 and t not in _TITLE_WORDS]
    own = set(re.findall(r"[\w-]+", norm_text(own_label)))
    if not otoks:
        return False, "other", None
    if set(otoks) <= own:
        return False, "self", None
    have = set(_plain(" ".join(excerpts)).split())
    if not any(t in have for t in otoks):
        return False, "other", None                       # the other person is not in the excerpts at all
    return True, "", (spos[idx] if spos and idx < len(spos) else None)


def profession_attached(prof, names, ev):
    """The book gives this profession to this person: "the pastor Aram", "Ter Haigasun, head priest",
    "Gabriel Bagradian was an Ottoman officer"; not merely a doctor somewhere in the same scene."""
    n = "|".join(re.escape(t) for t in names)
    return bool(re.search(rf"\b{prof}\b[\s,.]{{1,3}}(?:[\w-]+\s+)?(?:{n})\b", ev)
                or re.search(rf"\b(?:{n})\b(?:\s+was|\s+is|\s+became|\s+as|,|\s+\(|\s+-)\s*(?:the |a |an )?(?:[\w-]+\s+){{0,3}}{prof}\b", ev))


def drop_unattached_professions(text, names, ev):
    """-> (text without the sentences that give this person a profession the book never gives them, sentences dropped)"""
    if not text or not names:
        return text, 0
    keep, dropped = [], 0
    for sent in sentences(text):
        low = sent.lower()
        bad = any(re.search(rf"\b{p}\b", low) and not profession_attached(p, names, ev) for p in _PROFESSIONS)
        if bad:
            dropped += 1
        else:
            keep.append(sent)
    return " ".join(keep), dropped


def render_ties(ties, limit=TIE_LIMIT):
    """[(relation, other)] -> 'Father of Stephan Bagradian, husband of Juliette Bagradian.' closest ties first"""
    seen, keep = set(), []
    for rel, other in sorted(ties, key=lambda t: (_PRIORITY.get(t[0], 9), t[1])):
        if other in seen:
            continue
        seen.add(other)
        keep.append((rel, other))
        if len(keep) >= limit:
            break
    groups = {}
    for rel, other in keep:
        groups.setdefault(rel, []).append(other)
    parts = []
    for rel, others in groups.items():
        names = others[0] if len(others) == 1 else ", ".join(others[:-1]) + " and " + others[-1]
        parts.append(f"{rel} of {names}")
    s = ", ".join(parts)
    return (s[0].upper() + s[1:] + ".") if s else ""


def _restates_tie(part, ties):
    """a role part like "Gabriel's wife" that only says again what the ties line says"""
    words = set(re.findall(r"[\w-]+", part.lower()))
    if not (words & (_KIN_WORDS | {"parent", "child", "sibling"})):
        return False
    return any({t for t in re.findall(r"[\w-]+", other.lower()) if len(t) >= 3} & words for _rel, other in ties)


_KIN_STRICT = (set(KIN_RELATIONS) | {r + "s" for r in KIN_RELATIONS}
               | {r.replace("-", "") for r in KIN_RELATIONS} | {"married", "spouse", "widow", "widower"})


def _kin_claim_ok(text, tied):
    """a family word in the role or the progress is allowed only about someone the card has a checked tie to; the model
    wrote "Wife of Gabriel Bagradian" on Iskuhi's card and "Stephan's wife" on Hovsannah's, where no tie says so"""
    words = set(re.findall(r"[\w-]+", text.lower()))
    return not (words & _KIN_STRICT) or bool(words & tied)


def assemble_person_card(ties, role, progress):
    parts = []
    t = render_ties(ties)
    if t:
        parts.append(t)
    tied = {x for _r, other in ties for x in re.findall(r"[\w-]+", other.lower()) if len(x) >= 3}
    role = "; ".join(x for x in (y.strip() for y in (role or "").split(";"))
                     if x and not _restates_tie(x, ties) and _kin_claim_ok(x, tied))
    role = role.strip().rstrip(".")
    if role:
        parts.append(role[0].upper() + role[1:] + ".")
    prog = [x for x in sentences(progress or "") if _kin_claim_ok(x, tied)]
    while True:
        card = " ".join(parts + prog)
        if word_count(card) <= DESC_WORDS or not prog:
            return card
        prog.pop()                                        # the ceiling trims progress, never the ties or the role


def resolve_other(other, labels):
    """The entity a tie's other person is: the label that contains all of the given name's words and has by far the most
    mentions ('Stephan' -> 'Stephan Bagradian' rather than 'Young Stephan'); None if that is not clear."""
    toks = [t for t in re.findall(r"[\w-]+", norm_text(other)) if len(t) >= 3 and t not in _TITLE_WORDS]
    if not toks:
        return None
    if " ".join(toks) in SHARED_BARE:
        return None                                       # "Aureliano": which one? the sentence does not say
    cands = sorted(((m, l) for l, m in labels.items()
                    if l not in LOCKED_LABELS         # a split-off character ("Aureliano (81-99% of the book)") is never "the one a name means"
                    and set(toks) <= set(re.findall(r"[\w-]+", norm_text(l)))), reverse=True)
    if not cands:
        return None
    if len(cands) == 1 or cands[0][0] >= 2 * cands[1][0]:
        return cands[0][1]
    return None


def build_people_cards(people, gate=False):
    """people: name -> {label, count, rels: [{rel, other, pos}], rp: [(role, progress, pos)]}.
    Settles the ties of everyone together - each tie also appears on the other person's card, a pair that is told two
    different ways is dropped - then assembles every stage's card. gate=True (page-aware cards): a tie appears only on the
    pages from the point of the book where it is first stated. -> ({name: (pages, final card)}, counts)"""
    counts = {"ties": 0, "implied": 0, "unresolved": 0, "conflicts": 0, "resolved": 0, "minor": 0}
    labels, by_key = {}, {}
    for name, p in people.items():
        labels[p["label"]] = labels.get(p["label"], 0) + p["count"]
        by_key[name] = p["label"]

    def earlier(a, b):
        return a is None or (b is not None and a < b)

    # Entries the cast pass made for a character that shares a first name ("José Arcadio (58-91% of the book)") and the
    # bare entry that is left ("Aureliano") are not tied to anyone: a tie is found by a name or by the words around a
    # mention, and for these the name is exactly what is ambiguous (the eldest son's wife and mother turned up on his
    # great-great-grandson).
    def _bare(label):
        return " ".join(t for t in re.findall(r"[\w-]+", norm_text(label)) if len(t) >= 3 and t not in _TITLE_WORDS)
    blocked = {p["label"] for p in people.values()
               if p["label"] in LOCKED_LABELS or _bare(p["label"]) in SHARED_BARE or p["label"].strip().lower() in ADDRESS_WORDS}
    edges, weight = {}, collections.Counter()
    for p in people.values():
        a = p["label"]
        if a in blocked:
            continue
        for r in p["rels"]:
            b = by_key.get(r.get("other_key")) if r.get("other_key") else resolve_other(r["other"], labels)
            if b is None or b == a or b in blocked:
                counts["unresolved"] += 1
                continue
            weight[(a, b, r["rel"])] += r.get("n", 1)
            cur = edges.setdefault((a, b), {})
            if r["rel"] not in cur or earlier(r["pos"], cur[r["rel"]]):
                cur[r["rel"]] = r["pos"]
    votes = {}
    for (a, b), rels in edges.items():
        for rel in rels:
            for who, g in ((a, "m" if rel in _MALE else "f" if rel in _FEMALE else None),
                           (b, "f" if rel == "husband" else "m" if rel == "wife" else None)):
                if g:
                    votes.setdefault(who, collections.Counter())[g] += 1

    texts = {}
    for p in people.values():
        texts[p["label"]] = texts.get(p["label"], "") + " " + " ".join(f"{ro} {pr}" for ro, pr, _pos in p["rp"]).lower()

    def gender(label):
        # the pronouns of the person's own card ("He wears ...") first: a tie read the wrong way round ("Gabriel is
        # the wife of Juliette") must not decide who is a man
        t = texts.get(label, "")
        m = len(re.findall(r"\b(?:he|his|him|himself)\b", t))
        f = len(re.findall(r"\b(?:she|her|hers|herself)\b", t))
        if m >= 2 and m >= 3 * f:
            return "m"
        if f >= 2 and f >= 3 * m:
            return "f"
        v = votes.get(label)
        if v and not (len(v) == 2 and v["m"] == v["f"]):
            return "m" if v["m"] > v["f"] else "f"
        return None

    def drop_conflicts(table):
        """two kinds of tie between the same two people (father / son): the kind most sentences support stays when it
        has a strict majority; otherwise neither does"""
        for (a, b), rels in table.items():
            support = {}
            for r in rels:
                if r in _CATEGORY:
                    support[_CATEGORY[r]] = support.get(_CATEGORY[r], 0) + weight.get((a, b, r), 1)
            if len(support) > 1:
                ranked = sorted(support.items(), key=lambda kv: -kv[1])
                keep = ranked[0][0] if ranked[0][1] > ranked[1][1] else None
                for r in [r for r in rels if r in _CATEGORY and _CATEGORY[r] != keep]:
                    del rels[r]
                counts["resolved" if keep else "conflicts"] += 1

    drop_conflicts(edges)
    counts["ties"] = sum(len(r) for r in edges.values())
    full = {k: dict(v) for k, v in edges.items()}
    for (a, b), rels in edges.items():
        for rel, pos in rels.items():
            if rel not in _INVERSE:
                continue                                  # steward / servant have no inverse
            g = gender(b)
            inv = _INVERSE[rel][0 if g == "m" else 1 if g == "f" else 2]
            tgt = full.setdefault((b, a), {})
            weight[(b, a, inv)] += weight.get((a, b, rel), 1)
            if _CATEGORY.get(inv) and any(_CATEGORY.get(x) == _CATEGORY.get(inv) for x in tgt):
                continue                                  # b already states a tie of that kind
            if inv not in tgt:
                tgt[inv] = pos
                counts["implied"] += 1
    drop_conflicts(full)
    by_label = {}
    for (a, b), rels in full.items():
        if labels.get(b, 0) < TIE_MIN_MENTIONS:           # the other person is a minor entry ("Papa", "Ces")
            counts["minor"] = counts.get("minor", 0) + len(rels)
            continue
        ga = gender(a)
        for rel, pos in rels.items():
            if ga == "m" and rel in _FEMALE and rel in _FLIP:
                rel = _FLIP[rel]                          # "Wife of Juliette" on a man is "Husband of Juliette"
                counts["flipped"] = counts.get("flipped", 0) + 1
            elif ga == "f" and rel in _MALE and rel in _FLIP:
                rel = _FLIP[rel]
                counts["flipped"] = counts.get("flipped", 0) + 1
            if TIES_FROM_START and rel in KIN_RELATIONS:
                pos = None                                # a family tie is a fact about the person, not an event of the plot
            by_label.setdefault(a, []).append((rel, b, pos))
    out = {}
    for name, p in people.items():
        ties = by_label.get(p["label"], [])
        rp = p["rp"]
        pages, final = [], None
        if gate and rp and all(x[2] is not None for x in rp):
            # a new page starts at every stage and at every point where a tie is first stated, so "father of Stephan"
            # is on the card from the sentence that says it, not from the next stage boundary
            rps = sorted(rp, key=lambda x: x[2])
            events = sorted({x[2] for x in rps} | {tp for _r, _b, tp in ties if tp is not None})
            for e in events:
                cur = [x for x in rps if x[2] <= e]
                role, progress, _pos = cur[-1] if cur else rps[0]
                vis = [(rel, b) for rel, b, tp in ties if tp is None or tp <= e]
                card = assemble_person_card(vis, role, progress)
                if card and (not pages or card != pages[-1][0]):
                    pages.append((card, None if not pages else e))
                if card:
                    final = card
        else:
            for role, progress, pos in rp:
                vis = [(rel, b) for rel, b, tpos in ties if not gate or pos is None or tpos is None or tpos <= pos]
                card = assemble_person_card(vis, role, progress)
                if card and (not pages or card != pages[-1][0]):
                    pages.append((card, None if not pages else pos))
                if card:
                    final = card
        out[name] = (pages, final)
    return out, counts


# ---- fact check against Wikipedia (v1.2.11) ---------------------------------------------------------------------------
# Wikipedia is never a source of card text (the novel's article gives away the ending). It is read only to veto: the
# ties and roles the book evidence produced are put to the model together with the lines of the novel's article and of
# the characters' own pages that mention the person, and a claim the reference contradicts is removed. A verdict counts
# only if the model copies the reference sentence it relies on, exactly. What leaves the Mac: the book's title and the
# names of fictional characters, as page titles in requests to en.wikipedia.org. No text of the book.
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_CACHE_DAYS = 30
WIKI_MAX_PAGES = 30
WIKI_LINES = 10                  # reference lines shown per person
WIKI_BUDGET_SECONDS = 90         # give up fetching after this long


def wiki_cache_dir() -> Path:
    return Path.home() / ".claude-xray" / "cache" / "wikipedia"


def wiki_fetch_http(title: str, cache_dir=None, timeout: float = 15.0):
    """-> (plain text of the page, resolved title) or None. Cached for WIKI_CACHE_DAYS."""
    cache_dir = Path(cache_dir) if cache_dir else wiki_cache_dir()
    f = cache_dir / (re.sub(r"[^\w.-]+", "_", title)[:120] + ".json")
    try:
        if f.is_file() and time.time() - f.stat().st_mtime < WIKI_CACHE_DAYS * 86400:
            d = json.loads(f.read_text(encoding="utf-8"))
            return (d["text"], d["title"]) if d.get("text") else None
    except (OSError, ValueError, KeyError):
        pass
    q = urllib.parse.urlencode({"action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1,
                                "titles": title, "format": "json"})
    req = urllib.request.Request(WIKI_API + "?" + q, headers={"User-Agent": "EnhanceXRay/1.2 (personal calibre plugin)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            page = next(iter(json.load(r)["query"]["pages"].values()))
    except (urllib.error.URLError, OSError, ValueError, KeyError, StopIteration, http.client.HTTPException):
        return None
    text = page.get("extract") or ""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"title": page.get("title", title), "text": text}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return (text, page.get("title", title)) if text else None


def split_reference(text: str):
    """page text -> sentences (headings dropped; a 'Name - description' line of a character list stays whole)"""
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or re.match(r"^=+.*=+$", line):
            continue
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'])", line):
            if len(sent.strip()) >= 20:
                out.append(sent.strip())
    return out


def rank_reference(tokens, sentences, k=WIKI_LINES):
    """the reference sentences that mention the most of the given name words (family and job words break ties)"""
    toks = {t for t in tokens if len(t) >= 3}
    scored = []
    for i, sent in enumerate(sentences):
        words = set(re.findall(r"[\w-]+", re.sub(r"'s\b", "", norm_text(sent))))
        hit = len(toks & words)
        if hit:
            bonus = len(words & (_KIN_WORDS | {p for p in _PROFESSIONS}))
            scored.append((hit, bonus, -i, sent))
    scored.sort(reverse=True)
    return [s for _h, _b, _i, s in scored[:k]]


CHECK_SCHEMA = {
    "type": "object",
    "properties": {"entries": {"type": "array", "items": {
        "type": "object",
        "properties": {"claim": {"type": "integer"},
                       "verdict": {"type": "string", "enum": ["supported", "contradicted", "not_covered"]},
                       "evidence": {"type": "string"}},
        "required": ["claim", "verdict", "evidence"]}}},
    "required": ["entries"],
}
CHECK_FORMAT_HINT = ('\n\nReply with ONLY a JSON object, no prose, no code fences: {"entries": [{"claim": int, '
                     '"verdict": "supported"|"contradicted"|"not_covered", "evidence": str}, ...]}, one entry per numbered claim.')
CHECK_SYSTEM = (
    "You check claims about characters of a novel against reference text taken from Wikipedia. For each numbered claim "
    "answer: supported (the reference says it), contradicted (the reference says something that cannot be true together "
    "with the claim, for example a different family tie or a different occupation) or not_covered (the reference does not "
    "say). For supported and contradicted, give in evidence the reference sentence you rely on, copied exactly; for "
    "not_covered leave evidence empty. Use only the reference - never what you know of the book. A claim that merely adds "
    "detail the reference does not mention is not_covered, not contradicted.")


def check_prompt(label, claims, lines):
    p = "Reference text from Wikipedia (about the novel and its characters):\n" + "".join(f"- {x}\n" for x in lines)
    p += f"\nClaims about {label}, taken from the book:\n" + "".join(f"{i}. {c}\n" for i, c in enumerate(claims, 1))
    return p + "\nAnswer every numbered claim."


def accept_verdicts(entries, claims_meta, lines, label):
    """-> {claim number: ('supported' | 'contradicted', the reference sentence)}. A verdict counts only with the sentence quoted exactly,
    and a contradiction only if that sentence names the person or the other person of the claim."""
    ref = _plain(" ".join(lines))
    out = {}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        try:
            n = int(e.get("claim"))
        except (TypeError, ValueError):
            continue
        verdict, ev = e.get("verdict"), (e.get("evidence") or "").strip()
        if n < 1 or n > len(claims_meta) or verdict not in ("supported", "contradicted") or not ev:
            continue
        pe_ = _plain(ev)
        if not pe_ or pe_ not in ref:
            continue
        if verdict == "contradicted":
            toks = {t for t in re.findall(r"[\w-]+", norm_text(label)) if len(t) >= 3}
            toks |= {t for t in re.findall(r"[\w-]+", norm_text(claims_meta[n - 1].get("other") or "")) if len(t) >= 3}
            if not (toks & set(pe_.split())):
                continue
            if claims_meta[n - 1].get("kind") == "role":
                # a job or role is vetoed only by a sentence that is about this person: "Hrand Oskanian - School teacher in
                # conflict with Ter Haigasun" says nothing about Ter Haigasun's job
                own = {t for t in re.findall(r"[\w-]+", norm_text(label)) if len(t) >= 3 and t not in _TITLE_WORDS}
                if not (own & set(pe_.split()[:8])):
                    continue
        out[n] = (verdict, ev)
    return out


# ---- finding the ties in the book itself (v1.2.11) ---------------------------------------------------------------------
# The model rarely produces a usable tie while it writes a card from a handful of excerpts (13 of 65 proposals survived the
# checks). The book states the ties in a few sentences instead: a family word and two of the main people in one sentence.
# Code finds those sentences (Musa Dagger: 53 in the whole book, covering Gabriel/Stephan, Gabriel/Juliette, Aram/Iskuhi,
# Aram/Hovsannah ...), the model says how the two people are related according to that sentence only, and the sentence
# itself is the evidence.
TIE_MIN_MENTIONS = 20            # a person must have this many mentions to be a tie's other person ("Papa", "Ces" do not)
TIE_BATCH = 15                   # sentences per request
_KIN_SENT = re.compile(r"\b(?:son|daughter|wife|husband|brother|sister|father|mother|nephew|niece|uncle|aunt|cousin|"
                       r"grandfather|grandmother|grandson|granddaughter|married|spouse|child|children|parents?|widow)\b", re.I)
_CLAIM_WORDS = (set(KIN_RELATIONS) | set(_PROFESSIONS) | {"married", "spouse"})   # words that make a role part worth checking


def find_tie_candidates(work, occ, positions, texts, min_mentions=TIE_MIN_MENTIONS, max_len=420, window=2, per_pair=6):
    """Sentences of the book with a kin word (six languages; a title such as "Father Nicanor" does not count) that, together
    with the `window` - 1 sentences before it, name two or more of the main people; at most `per_pair` candidates for any
    pair (the earliest three and the rest spread over the book: a pair that is stated twenty times needs six, not twenty) (type 1, >= min_mentions). The window
    catches "Juliette came in. Her husband Gabriel ...", where the sentence with the kin word names only one of them.
    -> [{pos, sentence (the passage), trigger (the sentence with the kin word), names (most-mentioned first)}]"""
    people = {w["id"]: w for w in work if w["type"] == 1 and w["count"] >= min_mentions}
    starts = sorted((s, eid) for eid in people for s, _l in occ.get(eid, []))
    keys = [s for s, _ in starts]
    has_kin = kin_words.has_trigger if kin_words else (lambda t: bool(_KIN_SENT.search(t)))
    out = []
    for off, t in zip(positions, texts):
        sents = [(off + m.start(), off + m.end(), m.group(0)) for m in re.finditer(r"[^.!?]+[.!?]+[”\"’']?", t)]
        for i, (s0, s1, sent) in enumerate(sents):
            if len(sent) > max_len or not has_kin(sent):
                continue
            w0 = sents[max(0, i - window + 1)][0]
            names = {}
            for k in range(bisect.bisect_left(keys, w0), bisect.bisect_left(keys, s1)):
                w = people[starts[k][1]]
                names[w["name"]] = w["count"]
            if len(names) >= 2:
                in_sentence = len({people[starts[k][1]]["name"] for k in range(bisect.bisect_left(keys, s0), bisect.bisect_left(keys, s1))})
                passage = " ".join(" ".join(x[2].split()) for x in sents[max(0, i - window + 1):i + 1])
                out.append({"pos": s0, "sentence": passage[-900:], "trigger": " ".join(sent.split()),
                            "narrow": in_sentence >= 2,      # both people are named in the sentence with the family word
                            "names": sorted(names, key=lambda n: -names[n])[:3]})
    if per_pair:
        by_pair = {}
        for c in out:
            by_pair.setdefault(tuple(sorted(c["names"][:2])), []).append(c)
        keep = set()
        for lst in by_pair.values():
            if len(lst) <= per_pair:
                keep.update(id(c) for c in lst)
                continue
            head, rest = lst[:3], lst[3:]
            step = len(rest) / (per_pair - 3)
            keep.update(id(c) for c in head + [rest[int(i * step)] for i in range(per_pair - 3)])
        out = [c for c in out if id(c) in keep]
    return out


TIES_SCHEMA = {
    "type": "object",
    "properties": {"entries": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "relation": {"type": "string", "enum": list(KIN_RELATIONS) + ["none"]}},
        "required": ["id", "relation"]}}},
    "required": ["entries"],
}
TIES_FORMAT_HINT = ('\n\nReply with ONLY a JSON object, no prose, no code fences: {"entries": [{"id": int, "relation": str}, ...]}, '
                    'one entry per numbered item, relation being one of the listed words or "none".')
TIES_SYSTEM = (
    "You read short passages of a book and say how two named people are related. For each numbered item you get person A, "
    "person B and either one sentence or a passage with a key sentence: the key sentence is the one with the family word and "
    "must state the tie; the sentences before it only tell you who \"he\", \"she\" or \"his\" is. Answer with one word meaning \"A is the <word> of B\": "
    + ", ".join(KIN_RELATIONS) + " - or none. Give a word only if the sentence states the family tie between exactly A "
    "and B or plainly shows it (\"Gabriel gave his son Stephan an order\": Gabriel is the father of Stephan). Answer none "
    "when the sentence is about someone else's relatives, when the word is a form of address (a priest saying \"my son\"), a "
    "figure of speech, when A and B are not tied by the sentence, or when A or B is a place, a town, a group or a thing "
    "rather than a person (\"a son of Zeitun\" is a native of the town). Never use what you know of the book.")


def ties_prompt(items):
    p = BRIEF_SHORT + "Numbered items. Say how A is related to B according to the sentence only.\n\n"
    for i, it in enumerate(items, 1):
        if it.get("trigger") and it["trigger"] != it["sentence"]:
            p += (f"{i}. A = {it['a_show']}; B = {it['b_show']}\n   Passage: {it['sentence']}\n"
                  f"   Key sentence (the one with the family word): {it['trigger']}\n")
        else:
            p += f"{i}. A = {it['a_show']}; B = {it['b_show']}\n   Sentence: {it['sentence']}\n"
    return p


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"entries": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "verdict": {"type": "string", "enum": ["yes", "no"]}},
        "required": ["id", "verdict"]}}},
    "required": ["entries"],
}
VERIFY_FORMAT_HINT = ('\n\nReply with ONLY a JSON object, no prose, no code fences: {"entries": [{"id": int, "verdict": "yes"|"no"}, ...]}, '
                      'one entry per numbered item.')
VERIFY_SYSTEM = (
    "You check family ties that were read out of a book. For each numbered item you get person A, person B, the claimed tie "
    "(\"A is the <relation> of B\") and the quoted text it was taken from. Answer \"yes\" only if the quote itself states, or "
    "plainly shows, that A is the <relation> of B: the right two people, the right direction (not \"B is the <relation> of A\"), "
    "and exactly that relation (a sister-in-law is not a sister, a grandfather is not a father). Answer \"no\" when the quote "
    "is about someone else's relatives, is a form of address (a priest saying \"my son\", \"Papa\" said to the father), a "
    "figure of speech or hearsay about a third person, names the two people without tying them, or says something else. Use "
    "only the quote; never what you know of the book.")
TIE_VERIFY_BATCH = 12


def verify_prompt(items):
    p = BRIEF_SHORT + "Numbered items. Does the quote say it?\n\n"
    for i, it in enumerate(items, 1):
        p += f"{i}. Claim: {it['a_show']} is the {it['rel']} of {it['b_show']}.\n   Quote: {it['quote']}\n"
    return p


def apply_verdicts(items, answers):
    """Remove from their owners the ties the check answered "no" to. A tie with no (or a broken) answer stays: the check
    can only take a tie away on an explicit "no". -> the removed items"""
    removed = []
    for i, it in enumerate(items, 1):
        if answers.get(i) == "no":
            rels = it["owner"]["rels"]
            if it["tie"] in rels:
                rels.remove(it["tie"])
                removed.append(it)
    return removed


def tie_items(cands, shows):
    """candidate sentences -> one item per pair of people in the sentence (at most three pairs per sentence)"""
    items = []
    for c in cands:
        names = c["names"]
        for x in range(len(names)):
            for y in range(x + 1, len(names)):
                items.append({"a": names[x], "b": names[y], "pos": c["pos"], "sentence": c["sentence"], "trigger": c.get("trigger", c["sentence"]), "narrow": c.get("narrow", True),
                              "a_show": shows.get(names[x], names[x]), "b_show": shows.get(names[y], names[y])})
    return items


def split_top_tier(work):
    """-> (top, rest). Top = the TOP_TIER_SIZE most-mentioned entities having at least TOP_TIER_MIN_MENTIONS."""
    ordered = sorted(work, key=lambda w: -w["count"])
    top = [w for w in ordered[:TOP_TIER_SIZE] if w["count"] >= TOP_TIER_MIN_MENTIONS]
    top_ids = {w["id"] for w in top}
    return top, [w for w in ordered if w["id"] not in top_ids]


def _entity_chars(w) -> int:
    return (len(w["name"]) + 60 + sum(len(f) + 4 for f in w["excerpts"]) + len(w.get("card") or "")
            + len(str(w.get("known") or "")))


def pack_batches(entities, max_entities=BATCH_SIZE, max_chars=MAX_BATCH_CHARS):
    """Greedy batches of at most `max_entities` entities and about `max_chars` of prompt text."""
    batches, cur, size = [], [], 0
    for w in entities:
        c = _entity_chars(w)
        if cur and (len(cur) >= max_entities or size + c > max_chars):
            batches.append(cur)
            cur, size = [], 0
        cur.append(w)
        size += c
    if cur:
        batches.append(cur)
    return batches


def enrich_local(work, log, opts, book=None):
    base = opts.base_url.rstrip("/")
    pa = getattr(opts, "page_aware", "no") == "yes"
    key = load_omlx_key()
    if not opts.model_bulk:
        raise Skip("no local model chosen")
    high_model = opts.model_high or None
    if high_model == opts.model_bulk and getattr(opts, "same_model_tiers", "yes") == "no":
        high_model = None     # one model for everything: a single tier with 5 excerpts each (the v1.2.6 behaviour)
    if high_model:
        top, rest = split_top_tier(work)
        top = [dict(_take(w, FINE_EXCERPTS if pa and i < FINE_TOP else TOP_EXCERPTS, pa), fine=pa and i < FINE_TOP)
               for i, w in enumerate(top)]
        rest = [_take(w, EXCERPTS_PER_ENTITY, pa) for w in rest]
        tiers = [(high_model, top), (opts.model_bulk, rest)]
        avg = sum(len(w["excerpts"]) for w in top) / len(top) if top else 0
        log(f"  {len(top)} top-tier entities (top {TOP_TIER_SIZE} by mentions, at least {TOP_TIER_MIN_MENTIONS}; "
            f"avg {avg:.1f} excerpts each) -> {high_model}")
        log(f"  {len(rest)} others -> {opts.model_bulk}")
    else:
        tiers = [(opts.model_bulk, [_take(w, EXCERPTS_PER_ENTITY, pa) for w in work])]
        log(f"  {len(work)} entities -> {opts.model_bulk}")
    log(f"  server : {base}  (parallel {opts.parallel})")

    state = {"schema": True, "tool_choice": True, "abort": False}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    def one(model, batch, kind="term"):
        """-> (entries list or None, prompt_tokens, completion_tokens). kind: 'people' (role, progress, ties) or 'term'."""
        people = kind == "people"
        if kind == "check":
            sysmsg, schema, hint, user = CHECK_SYSTEM, CHECK_SCHEMA, CHECK_FORMAT_HINT, batch[0]["prompt"]
        elif kind == "ties":
            sysmsg, schema, hint, user = TIES_SYSTEM, TIES_SCHEMA, TIES_FORMAT_HINT, batch[0]["prompt"]
        elif kind == "verify":
            sysmsg, schema, hint, user = VERIFY_SYSTEM, VERIFY_SCHEMA, VERIFY_FORMAT_HINT, batch[0]["prompt"]
        elif people:
            sysmsg, schema, hint = (PEOPLE_SYSTEM_PA if pa else PEOPLE_SYSTEM), PEOPLE_SCHEMA, PEOPLE_FORMAT_HINT
            user = _people_prompt(batch, pa)
        else:
            sysmsg, schema, hint = (LOCAL_SYSTEM_PA if pa else LOCAL_SYSTEM), LOCAL_SCHEMA, LOCAL_FORMAT_HINT
            user = _batch_prompt(batch, pa)
        for attempt in range(MAX_RETRIES + 1):
            if state["abort"]:
                return None, 0, 0
            body = {
                "model": model, "temperature": 0.2, "max_tokens": 5000,
                "messages": [
                    {"role": "system", "content": sysmsg},
                    {"role": "user", "content": user + ("" if state["schema"] else hint)}],
                "chat_template_kwargs": {"enable_thinking": False},
            }
            if state["tool_choice"]:
                # oMLX merges its configured MCP tools into every chat request unless told
                # not to. A model may then answer with a tool call instead of JSON (seen with
                # the prompt-only fallback), and the tool definitions bloat every prompt.
                body["tool_choice"] = "none"
            if state["schema"]:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "entries", "schema": schema, "strict": True}}
            req = urllib.request.Request(f"{base}/chat/completions",
                                         json.dumps(body).encode("utf-8"), headers)
            try:
                with urllib.request.urlopen(req, timeout=LOCAL_TIMEOUT) as r:
                    resp = json.load(r)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    state["abort"] = True
                    raise Skip("the local server rejected the API key") from exc
                if exc.code == 404:
                    state["abort"] = True
                    raise Skip(f"model '{model}' not found on the local server") from exc
                detail = _error_text(exc)
                if exc.code in (400, 422):
                    if state["tool_choice"] and "tool_choice" in detail:
                        state["tool_choice"] = False  # strict server: tool_choice needs `tools`
                        continue
                    if state["schema"] and any(w in detail for w in _SCHEMA_WORDS):
                        state["schema"] = False       # the server itself says it cannot enforce a schema
                        log("  WARNING: the server says it cannot enforce a JSON schema; continuing without it. "
                            "Output is less reliable - use a server that supports structured output.")
                        continue
                    if not any(w in detail for w in _BUSY_WORDS):
                        continue                      # unexplained 400: retry as-is; never drop the schema on a guess
                # busy / short of memory (oMLX's prefill guard answers 400) or 429/5xx: wait
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                continue
            except urllib.error.URLError as exc:
                state["abort"] = True
                raise Skip(f"local server not reachable at {base} - is oMLX running?") from exc
            except (OSError, ValueError, http.client.HTTPException):
                continue                              # timeout / dropped connection / short read: retry
            content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            parsed = extract_json(content)
            entries = parsed.get("entries") if isinstance(parsed, dict) else None
            if isinstance(entries, list):
                u = resp.get("usage") or {}
                return entries, u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
        return None, 0, 0

    # Memory lifecycle for the BULK model only (oMLX: POST /v1/models/{id}/load and /unload). The top-tier model
    # is never touched here - it stays resident (it is often pinned for other tools). The bulk model is loaded
    # up front, and unloaded when the run ends if this run loaded it and it is not pinned. Every call is
    # non-fatal; a server without these endpoints is simply left to manage its own models.
    unload_bulk = getattr(opts, "unload_bulk", "yes") == "yes"
    life = {"by_us": False, "pinned": False}

    def prepare_bulk(model):
        st = omlx_model_state(base, headers, model)
        if st is None:
            return
        life["pinned"] = st["pinned"]
        if st["loaded"]:
            log(f"  {model} is already resident; it will be left loaded")
            return
        life["by_us"] = True
        log(f"  loading {model} ...")
        t = time.time()
        code, _ = _omlx_call(base, headers, f"/models/{quote(model, safe='')}/load", "POST", timeout=LOCAL_TIMEOUT)
        log(f"  {model} loaded in {time.time() - t:.0f} s" if code == 200
            else f"  could not pre-load {model} (HTTP {code}); the first batch will load it")

    def release_bulk(model):
        if not life["by_us"]:
            return
        if not unload_bulk:
            log(f"  {model} left loaded (unload when finished is off)")
        elif life["pinned"]:
            log(f"  {model} is pinned in oMLX; left loaded")
        else:
            code, _ = _omlx_call(base, headers, f"/models/{quote(model, safe='')}/unload", "POST", timeout=120)
            if code == 200:
                log(f"  {model} unloaded; memory returned to oMLX")
            elif code == 202:        # oMLX still holds the last request's lease: unload is queued and follows within seconds
                log(f"  {model} unload queued; memory returns as soon as its last request drains")
            else:
                log(f"  could not unload {model} (HTTP {code}); oMLX will unload it when idle")

    # Layered cards: every entity carries a state through its stages (see LAYER_* above). Each stage gets the
    # card so far plus the next excerpts and writes the updated card; the model decides what to keep, fold
    # into history or drop. st["stages"] keeps the card after every stage with the position of that stage's
    # newest excerpt, which page-aware mode uses to decide where in the book each card is shown.
    people_on = getattr(opts, "people_layout", "yes") == "yes"
    states = {w["name"]: {"card": None, "name": None, "names": [], "core": None, "seen": "", "stages": [], "trail": [],
                          "people": people_on and w["type"] == 1, "count": w["count"], "role": "", "progress": "",
                          "rels": [], "rp": [], "surface": w.get("surface") or "", "wiki": []}
              for _m, sub in tiers for w in sub}
    stats = {"accepted": 0, "condensed": 0, "rejected_long": 0, "rejected_meta": 0, "rejected_spoiler": 0,
             "rejected_relations": 0, "scrubbed": 0, "relations": 0, "names_rejected": 0, "empty": 0, "failed": 0,
             "core_dropped": 0, "ties_proposed": 0, "ties_ok": 0, "professions": 0, "tie_reject": collections.Counter(),
             "wiki_checked": 0, "wiki_supported": 0, "wiki_ties_removed": 0, "wiki_roles_removed": 0,
             "scan_asked": 0, "scan_ties": 0, "scan_sentences": 0, "wiki_unconfirmed": 0}

    def vote_name(cname, w):
        """The name this stage votes for. A proposal the book never uses is replaced by the entity's own label - or, when
        that label is itself not something the book says (a chapter title that tags a character), by the word the reader
        sees highlighted."""
        if not cname:
            return w["name"]
        if name_attested(cname, w["name"], book):
            return cname
        stats["names_rejected"] += 1
        surf = w.get("surface") or ""
        if book is not None and surf and surf != norm_text(w["name"]) and book.count(norm_text(w["name"])) < 3:
            return surf.title()
        return w["name"]
    ok_batches = all_batches = 0

    def trim_to_limit(text):
        """Drop whole trailing sentences until the card fits; None if it cannot."""
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        while len(parts) > 1 and word_count(" ".join(parts)) > DESC_WORDS:
            parts.pop()
        out = " ".join(parts)
        return out if word_count(out) <= DESC_WORDS else None

    def take(w, stage_no, e):
        """Fold one stage's reply into the entity's state; a bad or empty reply keeps the earlier card."""
        st = states[w["name"]]
        if e is None:
            stats["failed"] += 1
            st["trail"].append({"stage": stage_no, "status": "failed"})
        else:
            raw = (e.get("description") or "").strip()
            cname = (e.get("canonical_name") or "").strip()
            desc, status = raw, "accepted"
            if not raw:
                status = "empty"
            elif _META.search(desc):
                status = "rejected_meta"
            else:
                # page-aware: an outcome is fine if the excerpts shown so far contain it; static: never
                seen_all = st["seen"] + " " + " ".join(w["excerpts"])
                desc, dropped = scrub(desc, seen_all if pa else None)
                if desc is not None:
                    desc, rel = guard_relations(desc, seen_all, w["name"])
                    stats["relations"] += rel
                    if desc is None:
                        status = "rejected_relations"
                if desc is None:
                    status = status if status == "rejected_relations" else "rejected_spoiler"
                else:
                    stats["scrubbed"] += dropped
                    if word_count(desc) > DESC_WORDS:
                        st["over"] = {"card": desc, "name": cname}   # the model shortens it after this stage
                        status = "condensing"
            rec = {"stage": stage_no, "status": status, "card": raw, "name": cname}
            if status == "accepted":
                if st["core"] is None:
                    st["core"] = core_anchors(desc, w["name"])
                elif not keeps_core(desc, st["core"]):
                    stats["core_dropped"] += 1          # reported only; the model has the final say
                    rec["core_kept"] = False
                st["card"], st["name"] = desc, cname or st["name"]
                st["names"].append(vote_name(cname, w))
            if status != "condensing":
                stats[status] += 1
            st["trail"].append(rec)
        st["seen"] += " " + " ".join(w["excerpts"])
        spos = w.get("spos") or []
        st["stages"].append((st["card"], spos[-1] if spos else None))

    def take_person(w, stage_no, e):
        """Fold one stage's reply for a person into their state: ties (each backed by a quote from the excerpts), role,
        progress. Anything that fails a check is left out; the earlier role and progress stay."""
        st = states[w["name"]]
        spos = w.get("spos") or []
        if e is None:
            stats["failed"] += 1
            st["trail"].append({"stage": stage_no, "status": "failed"})
        else:
            cname = (e.get("canonical_name") or "").strip()
            seen_all = st["seen"] + " " + " ".join(w["excerpts"])
            ev = book if book is not None else norm_text(seen_all)
            names = [t for t in re.findall(r"[\w-]+", norm_text(w["name"] + " " + (w.get("surface") or "")))
                     if len(t) >= 3 and t not in _TITLE_WORDS]

            def clean(text, limit):
                text = (text or "").strip()
                if not text:
                    return ""
                if _META.search(text):
                    stats["rejected_meta"] += 1
                    return ""
                text, dropped = scrub(text, seen_all if pa else None)
                stats["scrubbed"] += dropped
                if text is None:
                    return ""
                text, rel = guard_relations(text, seen_all, w["name"])
                stats["relations"] += rel
                if text is None:
                    return ""
                text, prof = drop_unattached_professions(text, names, ev)
                stats["professions"] += prof
                parts = sentences(text)
                while len(parts) > 1 and word_count(" ".join(parts)) > limit:
                    parts.pop()
                text = " ".join(parts)
                return text if text and word_count(text) <= limit else ""

            role, progress = clean(e.get("role"), ROLE_WORDS), clean(e.get("progress"), PROGRESS_WORDS)
            new_ties, rejected = [], []
            for r in (e.get("relations") or []):
                if not isinstance(r, dict):
                    continue
                stats["ties_proposed"] += 1
                ok, why, pos = valid_tie(r.get("relation"), r.get("other"), r.get("quote"), w["name"], w["excerpts"], spos)
                if not ok:
                    stats["tie_reject"][why] += 1
                    rejected.append({"tie": f"{r.get('relation')} of {r.get('other')}", "why": why})
                    continue
                tie = {"rel": r["relation"].strip().lower(), "other": r["other"].strip(), "other_key": None, "pos": pos,
                       "quote": r["quote"].strip(), "stage": stage_no}
                if not any(t["rel"] == tie["rel"] and norm_text(t["other"]) == norm_text(tie["other"])
                           for t in st["rels"] + new_ties):
                    new_ties.append(tie)
            st["rels"].extend(new_ties)
            stats["ties_ok"] += len(new_ties)
            if role:
                st["role"] = role
            if progress:
                st["progress"] = progress
            status = "accepted" if (role or progress or new_ties) else "empty"
            if status == "accepted":
                st["names"].append(vote_name(cname, w))
            stats[status] += 1
            st["trail"].append({"stage": stage_no, "status": status, "name": cname, "role": role, "progress": progress,
                                "ties": [f"{t['rel']} of {t['other']}" for t in new_ties], "ties_rejected": rejected})
        st["seen"] += " " + " ".join(w["excerpts"])
        st["rp"].append((st["role"], st["progress"], spos[-1] if spos else None))

    def condense_round(model, over_items, stage_no):
        """Cards that came back over the word ceiling: the model shortens them (keeping who they are and the newest
        facts). If it still cannot, whole trailing sentences are dropped. Cutting blindly would lose the newest facts
        of a card that grows by appending."""
        cbatches = pack_batches([dict(w, excerpts=[], card=states[w["name"]]["over"]["card"], condense=True)
                                 for w in over_items])
        with ThreadPoolExecutor(max(1, opts.parallel)) as cpool:
            futs = [cpool.submit(one, model, b) for b in cbatches]
            for b, fut in zip(cbatches, futs):
                entries, _pin, _pout = fut.result()
                by = {e.get("original_name"): e for e in (entries or []) if isinstance(e, dict)}
                for i, w in enumerate(b):
                    e = by.get(w["name"])
                    if e is None and entries and len(entries) == len(b) and isinstance(entries[i], dict):
                        e = entries[i]
                    st = states[w["name"]]
                    over = st.pop("over")
                    cand = ((e or {}).get("description") or "").strip()
                    if not cand or word_count(cand) > DESC_WORDS or _META.search(cand):
                        cand = trim_to_limit(over["card"]) or ""
                    dropped = 0
                    if cand:
                        cand, dropped = scrub(cand, st["seen"] if pa else None)
                        stats["scrubbed"] += dropped
                        if cand:
                            cand, rel = guard_relations(cand, st["seen"], w["name"])
                            stats["relations"] += rel
                    rec = {"stage": stage_no, "card": cand or "", "name": over["name"]}
                    if not cand:
                        rec["status"] = "rejected_long"
                        stats["rejected_long"] += 1
                    else:
                        rec["status"] = "condensed"
                        stats["condensed"] += 1
                        if st["core"] is None:
                            st["core"] = core_anchors(cand, w["name"])
                        st["card"], st["name"] = cand, over["name"] or st["name"]
                        st["names"].append(vote_name(over["name"], w))
                        st["stages"][-1] = (cand, st["stages"][-1][1])
                    st["trail"].append(rec)

    wiki_on = getattr(opts, "wiki_check", "yes") == "yes" and people_on

    def tie_pass(model):
        """Say, for every sentence of the book that has a family word and two of the main people, how they are related."""
        cands = list(getattr(opts, "tie_candidates", None) or [])
        if not cands:
            return
        shows = {}
        for st_name, st_ in states.items():
            surf = st_["surface"]
            shows[st_name] = st_name if not surf or surf == norm_text(st_name) else f"{st_name} (shown in the book as \"{surf}\")"
        items = [it for it in tie_items(cands, shows) if states.get(it["a"], {}).get("people") and states.get(it["b"], {}).get("people")]
        batches = [items[i:i + TIE_BATCH] for i in range(0, len(items), TIE_BATCH)]
        accepted = asked = 0
        with ThreadPoolExecutor(max(1, opts.parallel)) as tpool:
            futs = [tpool.submit(one, model, [{"prompt": ties_prompt(b)}], "ties") for b in batches]
            for b, fut in zip(batches, futs):
                entries, _pin, _pout = fut.result()
                answer = {}
                for e in entries or []:
                    if isinstance(e, dict):
                        try:
                            answer[int(e.get("id"))] = (e.get("relation") or "").strip().lower()
                        except (TypeError, ValueError):
                            pass
                for i, it in enumerate(b, 1):
                    asked += 1
                    rel = answer.get(i)
                    trig = it.get("trigger") or it["sentence"]
                    if rel not in KIN_RELATIONS or not (set(_plain(trig).split()) & _REL_GROUP[_CATEGORY[rel]]
                                                        or _foreign_kin(rel, trig)):
                        continue
                    st_a = states[it["a"]]
                    same = [t for t in st_a["rels"] if t["rel"] == rel and t.get("other_key") == it["b"]]
                    if same:
                        same[0]["n"] = same[0].get("n", 1) + 1          # another sentence says it too
                        same[0]["narrow"] = same[0].get("narrow", True) or it.get("narrow", True)
                        same[0]["pos"] = it["pos"] if (same[0]["pos"] is None or it["pos"] < same[0]["pos"]) else same[0]["pos"]
                        continue
                    st_a["rels"].append({"rel": rel, "other": it["b"], "other_key": it["b"], "pos": it["pos"],
                                         "quote": it["sentence"], "stage": 0, "n": 1, "narrow": it.get("narrow", True)})
                    accepted += 1
        # A tie found only through the wider window ("Juliette came in. Her husband ...") is kept when two sentences say it,
        # and an in-law tie from it never: "asked Iskuhi about her sister-in-law" is about a third person (Musa Dagh run:
        # "Shushik, mother of Stephan", "Gabriel, brother-in-law of Iskuhi").
        dropped = 0
        for st_ in states.values():
            keep = []
            for t_ in st_["rels"]:
                if t_.get("stage") == 0 and t_.get("narrow") is False and (t_.get("n", 1) < 2 or _CATEGORY.get(t_["rel"], "").startswith("inlaw")):
                    dropped += 1
                    accepted -= 1
                    continue
                keep.append(t_)
            st_["rels"][:] = keep
        stats["ties_ok"] += accepted
        stats["scan_wide_dropped"] = dropped
        stats["scan_asked"], stats["scan_ties"], stats["scan_sentences"] = asked, accepted, len(cands)

    def tie_verify(model):
        """A second, independent look at every tie before it reaches a card: the quote it rests on is shown to the model next to
        the claim and it says yes or no. The scan and the card requests each make one guess; this asks only "does this
        text say that?", which is a much easier question (a run of 1.2.13 had "Thomas Kebussyan, husband of Mairik
        Antaram" from a sentence that says Antaram is the doctor's wife)."""
        shows = {}
        for st_name, st_ in states.items():
            surf = st_["surface"]
            shows[st_name] = st_name if not surf or surf == norm_text(st_name) else f"{st_name} (shown in the book as \"{surf}\")"
        items = []
        for st_name, st_ in states.items():
            if not st_["people"]:
                continue
            for t_ in st_["rels"]:
                q_ = " ".join((t_.get("quote") or "").split())
                if q_:
                    key_ = t_.get("other_key") or t_["other"]
                    items.append({"owner": st_, "tie": t_, "rel": t_["rel"], "quote": q_[-800:], "a_show": shows.get(st_name, st_name),
                                  "b_show": shows.get(key_, t_["other"]), "a": st_name})
        if not items:
            return
        batches = [items[i:i + TIE_VERIFY_BATCH] for i in range(0, len(items), TIE_VERIFY_BATCH)]
        removed = []
        with ThreadPoolExecutor(max(1, opts.parallel)) as vpool:
            futs = [vpool.submit(one, model, [{"prompt": verify_prompt(b)}], "verify") for b in batches]
            for b, fut in zip(batches, futs):
                entries, _pin, _pout = fut.result()
                answers = {}
                for e in entries or []:
                    if isinstance(e, dict):
                        try:
                            answers[int(e.get("id"))] = (e.get("verdict") or "").strip().lower()
                        except (TypeError, ValueError):
                            pass
                removed.extend(apply_verdicts(b, answers))
        stats["tie_checked"], stats["tie_removed"] = len(items), len(removed)
        for it in removed[:6]:
            log(f"    tie check removed: {it['a']} {it['rel']} of {it['tie']['other']}  <- {it['quote'][:110]}")

    def wiki_fact_check(model):
        """Put every person's ties and role to the Wikipedia reference; a contradicted claim is removed."""
        titles = []
        for t in [getattr(opts, "title", "")] + list(getattr(opts, "wiki_pages", None) or []):
            if t and t not in titles:
                titles.append(t)
        titles = titles[:WIKI_MAX_PAGES]
        if not titles:
            return
        fetch = getattr(opts, "wiki_fetch", None) or wiki_fetch_http
        t0, ref, got = time.time(), [], []
        for t in titles:
            if time.time() - t0 > WIKI_BUDGET_SECONDS:
                break
            r = fetch(t)
            if r:
                got.append(r[1])
                ref.extend(split_reference(r[0]))
        if not ref:
            log("  Wikipedia fact check skipped: no page text could be fetched (offline, or no page for this title)")
            return
        jobs = []
        for name, st in states.items():
            if not st["people"]:
                continue
            label = settle_name(st["names"], name)
            claims, meta = [], []
            for t in st["rels"]:
                claims.append(f"{label} is the {t['rel']} of {t['other']}.")
                meta.append({"kind": "tie", "tie": t, "other": t["other"]})
            for part in [x.strip() for x in st["role"].split(";") if x.strip()]:
                if set(re.findall(r"[\w-]+", part.lower())) & _CLAIM_WORDS:      # a claim about family or a job
                    claims.append(f"{label}: {part}")
                    meta.append({"kind": "role", "part": part, "other": ""})
            if not claims:
                continue
            toks = [x for x in re.findall(r"[\w-]+", norm_text(label + " " + st["surface"] + " "
                                                               + " ".join(m["other"] for m in meta)))
                    if len(x) >= 3 and x not in _TITLE_WORDS]
            lines = rank_reference(toks, ref)
            if lines:
                jobs.append((name, label, claims, meta, lines))
        with ThreadPoolExecutor(max(1, opts.parallel)) as cpool:
            futs = [cpool.submit(one, model, [{"prompt": check_prompt(label, claims, lines)}], "check")
                    for _n, label, claims, _m, lines in jobs]
            for (name, label, claims, meta, lines), fut in zip(jobs, futs):
                entries, _pin, _pout = fut.result()
                stats["wiki_checked"] += 1
                st = states[name]
                def role_without(role, part):
                    return "; ".join(x for x in (y.strip() for y in role.split(";")) if x and x != part)

                verdicts = accept_verdicts(entries, meta, lines, label)
                raw = {}
                for e_ in entries or []:
                    if isinstance(e_, dict):
                        try:
                            raw[int(e_.get("claim"))] = e_.get("verdict")
                        except (TypeError, ValueError):
                            pass
                if entries is not None:
                    # a tie backed by a single sentence goes if the judge says the reference does not cover it although the reference
                    # has lines about this person ("Gabriel, brother of Pastor Aram", from one sentence, is more likely a
                    # misreading than a fact the reference forgot). A supported or unclear verdict keeps it: a judge whose quote
                    # does not match exactly must not cost a true tie
                    for n, m in enumerate(meta, 1):
                        if (m["kind"] == "tie" and m["tie"].get("n", 1) == 1 and raw.get(n) == "not_covered"
                                and m["tie"] in st["rels"]):
                            st["rels"].remove(m["tie"])
                            stats["wiki_unconfirmed"] += 1
                            st["wiki"].append({"removed_tie": claims[n - 1], "evidence": "single sentence, not confirmed"})
                for n, (verdict, ev) in verdicts.items():
                    m = meta[n - 1]
                    if verdict == "supported":
                        stats["wiki_supported"] += 1
                    elif m["kind"] == "tie":
                        if m["tie"] in st["rels"]:
                            st["rels"].remove(m["tie"])
                            stats["wiki_ties_removed"] += 1
                            st["wiki"].append({"removed_tie": claims[n - 1], "evidence": ev})
                    elif m["part"] in st["role"]:
                        st["role"] = role_without(st["role"], m["part"])
                        st["rp"] = [(role_without(r, m["part"]), pr, pos) for r, pr, pos in st["rp"]]
                        stats["wiki_roles_removed"] += 1
                        st["wiki"].append({"removed_role": m["part"], "evidence": ev})
        log(f"  Wikipedia fact check: {len(got)} page(s) read ({', '.join(got[:4])}{' ...' if len(got) > 4 else ''}), "
            f"{stats['wiki_checked']} people checked, {stats['wiki_supported']} claims supported, "
            f"{stats['wiki_ties_removed']} ties and {stats['wiki_roles_removed']} roles removed because the reference "
            f"contradicts them, {stats['wiki_unconfirmed']} ties from a single sentence removed because the reference "
            f"does not confirm them (a verdict counts only with the reference sentence quoted exactly)")

    def save_checkpoint():
        cp = getattr(opts, "checkpoint", None)
        if not cp:
            return
        try:
            Path(cp).parent.mkdir(parents=True, exist_ok=True)
            tmp_cp = Path(str(cp) + ".tmp")
            tmp_cp.write_text(json.dumps(
                {n: ({"card": s_["card"], "name": s_["name"], "stages": s_["stages"], "trail": s_["trail"]}
                     if not s_["people"] else
                     {"people": True, "name": s_["name"], "role": s_["role"], "progress": s_["progress"], "ties": s_["rels"],
                      "rp": s_["rp"], "wiki": s_["wiki"], "trail": s_["trail"]})
                 for n, s_ in states.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp_cp.replace(cp)
        except OSError:
            pass                                       # the checkpoint is a debugging aid only

    try:
        for model, subset in tiers:
            if not subset:
                continue
            if model == opts.model_bulk and not life.get("prepared"):
                life["prepared"] = True               # once, even when the top tier uses the same model
                prepare_bulk(model)
            plan = {}
            for w in subset:
                chunks = split_stages(w["excerpts"], pa, w.get("fine", False))
                sp, k = w.get("spos") or [], 0
                pchunks = []
                for c in chunks:
                    pchunks.append(sp[k:k + len(c)])
                    k += len(c)
                plan[w["name"]] = list(zip(chunks, pchunks))
            n_stages = max(len(c) for c in plan.values())
            for stage_i in range(n_stages):
                def known_of(w):
                    st_ = states[w["name"]]
                    if not st_["people"] or not (st_["role"] or st_["progress"] or st_["rels"]):
                        return None
                    return {"role": st_["role"], "progress": st_["progress"],
                            "ties": [f"{t['rel']} of {t['other']}" for t in st_["rels"]]}

                items = [dict(w, excerpts=plan[w["name"]][stage_i][0], spos=plan[w["name"]][stage_i][1],
                              card=states[w["name"]]["card"], known=known_of(w))
                         for w in subset if stage_i < len(plan[w["name"]])]
                jobs = ([(b, "people") for b in pack_batches([it for it in items if states[it["name"]]["people"]],
                                                              max_entities=PEOPLE_BATCH)]
                        + [(b, "term") for b in pack_batches([it for it in items if not states[it["name"]]["people"]])])
                all_batches += len(jobs)
                tok_in = tok_out = done = 0
                with ThreadPoolExecutor(max(1, opts.parallel)) as pool:
                    futures = [pool.submit(one, model, b, kind) for b, kind in jobs]
                    for n, ((batch, kind), fut) in enumerate(zip(jobs, futures), 1):
                        fold = take_person if kind == "people" else take
                        entries, pin, pout = fut.result()          # a Skip raised in a worker surfaces here
                        if entries is None:
                            log(f"  {model} batch {n}/{len(jobs)}: no valid reply, skipped")
                            for w in batch:
                                fold(w, stage_i + 1, None)
                            continue
                        by_name = {e.get("original_name"): e for e in entries if isinstance(e, dict)}
                        for i, w in enumerate(batch):
                            e = by_name.get(w["name"])
                            if e is None and len(entries) == len(batch) and isinstance(entries[i], dict):
                                e = entries[i]                     # model altered the echoed name: use order
                            fold(w, stage_i + 1, e)
                        tok_in += pin
                        tok_out += pout
                        done += 1
                        ok_batches += 1
                        if done % 10 == 0:
                            log(f"  {model}: {done}/{len(jobs)} batches done")
                over_items = [w for w in items if "over" in states[w["name"]]]
                if over_items:
                    condense_round(model, over_items, stage_i + 1)
                tag = f" stage {stage_i + 1}/{n_stages}" if n_stages > 1 else ""
                log(f"  {model}{tag}: {done}/{len(jobs)} batches ({len(items)} entities), "
                    f"in={tok_in:,} out={tok_out:,} tok")
                save_checkpoint()
        if people_on:
            try:
                tie_pass(high_model or opts.model_bulk)
            except Skip:
                raise
            except Exception as exc:
                log(f"  tie scan skipped ({exc.__class__.__name__}: {exc})")
        if people_on and getattr(opts, "tie_verify", "yes") == "yes":
            try:
                tie_verify(high_model or opts.model_bulk)
            except Skip:
                raise
            except Exception as exc:                       # the check can only remove ties; a failure leaves them
                log(f"  tie check skipped ({exc.__class__.__name__}: {exc})")
        if wiki_on:
            try:
                wiki_fact_check(high_model or opts.model_bulk)
            except Skip:
                raise
            except Exception as exc:                       # the fact check must never lose the run
                log(f"  Wikipedia fact check skipped ({exc.__class__.__name__}: {exc})")

    finally:
        release_bulk(opts.model_bulk)

    results: dict[str, SimpleNamespace] = {}
    flips = 0
    for name, st in states.items():
        if len(set(st["names"])) > 1:
            flips += 1
        st["name"] = settle_name(st["names"], name)           # by vote, not by the last stage
    people = {name: {"label": st["name"] or name, "count": st["count"], "rels": st["rels"], "rp": st["rp"]}
              for name, st in states.items() if st["people"]}     # everyone: a person with nothing of their own can
                                                                  # still be named by another's tie and get theirs back
    built, tie_counts = build_people_cards(people, gate=pa)
    for name, st in states.items():
        if st["people"]:
            if name in built and built[name][1]:
                pages, final = built[name]
                results[name] = SimpleNamespace(original_name=name, canonical_name=st["name"] or name,
                                                description=final, grounded=True, stages=pages)
        elif st["card"]:
            pages = []                                 # distinct cards in order, each with where it starts
            for card, pos in st["stages"]:
                if card and (not pages or card != pages[-1][0]):
                    pages.append((card, None if not pages else pos))
            results[name] = SimpleNamespace(original_name=name, canonical_name=st["name"] or name,
                                            description=st["card"], grounded=True, stages=pages)
    if flips:
        log(f"  {flips} entities were given different names at different stages; each keeps the name most stages agreed on "
            f"(or its own label when there is no clear majority)")
    save_checkpoint()
    if people:
        if stats.get("tie_checked"):
            log(f"  tie check: {stats['tie_checked']} ties looked at a second time against the text they rest on, "
                f"{stats['tie_removed']} removed because the text does not say it")
        if stats["scan_sentences"]:
            log(f"  tie scan: {stats['scan_sentences']} sentences of the book have a family word and two of the main people; "
                f"{stats['scan_ties']} of {stats['scan_asked']} pairs were classified as a family tie")
        rej = stats["tie_reject"]
        log(f"  people: {len(people)} cards in the fixed layout (ties, role, progress); ties: {stats['ties_proposed']} proposed, "
            f"{stats['ties_ok']} backed by a quote from the excerpts (rejected: {rej['quote']} quote not in the excerpts, "
            f"{rej['word']} no family word in the quote, {rej['other']} other person not in the excerpts, {rej['self']} "
            f"the person themselves, {rej['relation']} unknown relation), {tie_counts['implied']} more implied by the other "
            f"person's card, {tie_counts['resolved']} pairs told two ways settled by the majority of the sentences, "
            f"{tie_counts['conflicts']} dropped because the sentences split evenly, "
            f"{tie_counts['unresolved']} ties dropped because the other person could not be identified, "
            f"{tie_counts['minor']} because the other person is a minor entry (under {TIE_MIN_MENTIONS} mentions); "
            f"{stats['professions']} sentences dropped for a profession the book never gives that person")
    log(f"  cards: {stats['accepted'] + stats['condensed']} written ({stats['condensed']} of them shortened by the model "
        f"after coming back over {DESC_WORDS} words), {stats['rejected_long']} kept an earlier card, {stats['rejected_meta']} dropped (commented on the text), {stats['rejected_spoiler']} dropped "
        f"(nothing left after the spoiler guard), {stats['scrubbed']} sentences removed by the spoiler guard, "
        f"{stats['relations']} sentences removed for naming a relative the excerpts never mention "
        f"({stats['rejected_relations']} cards dropped whole), {stats['names_rejected']} proposed names dropped because the book never uses them, "
        f"{stats['empty']} not identified, {stats['failed']} failed; {stats['core_dropped']} later cards no longer "
        f"mention some names from the first card")

    if not results:
        raise Skip("no batches succeeded")
    log(f"  enriched {len(results)} entities in {ok_batches}/{all_batches} batches  (local, no cost)")
    return results


def enrich(work, log, opts, book=None):
    if opts.provider == "local":
        return enrich_local(work, log, opts, book=book)
    return enrich_claude(work, log, opts)


# --------------------------------------------------------------------------
# apply (writes to a copy, then swaps)
# --------------------------------------------------------------------------

def canonical_names(work, results):
    """entity id -> the name the model settled on (only where it is grounded and non-empty)"""
    canon = {}
    for w in work:
        r = results.get(w["name"])
        if w["name"] in LOCKED_LABELS:
            if r and r.grounded:
                canon[w["id"]] = w["name"]        # keeps its own label (never renamed) but still gets its card written
            continue
        if r and r.grounded and r.canonical_name.strip():
            canon[w["id"]] = r.canonical_name.strip()
    return canon


def name_groups(ents, canon):
    """(name, type) -> ids sharing it after renaming. Keyed over EVERY entity, not just the enriched ones:
    renaming "Z. Stalin" to "Stalin" has to find the existing "Stalin" row."""
    groups = {}
    for eid, e in ents.items():
        groups.setdefault((canon.get(eid, e["label"]).lower(), e["type"]), []).append(eid)
    return groups


def same_name_longer(old: str, new: str) -> bool:
    """True when `new` is `old` made longer or re-titled, i.e. the same name and so (almost surely) the same person:
    "Morel" -> "E. D. Morel", "Casement" -> "Roger Casement", "King Leopold" -> "Leopold II". Titles and numerals
    are ignored on both sides. "Yakov Stalin" -> "Stalin" is not (shorter: two different men), nor is
    "Stephan Sets Out" -> "Stephan Bagradian" (the words differ)."""
    try:
        from cast_pass import core_tokens
    except ImportError:                                   # a worker without the cast pass module
        core_tokens = str.split
    a = [t.lower().strip(".") for t in core_tokens(old)]
    b = [t.lower().strip(".") for t in core_tokens(new)]
    if not a or not b or len(b) < len(a):
        return False
    return b[:len(a)] == a or b[-len(a):] == a


def find_collisions(ents, canon, groups, rescue=True):
    """Renames that land on another entity's name. -> [{key, sources, targets, rescued, rescued_source}].
    A source is rescued (renamed and merged even with merging off) when it is plainly the entity the target names,
    only badly labelled: it has at least RESCUE_RATIO times the target's mentions, no target is Wikipedia-described,
    and the words highlighted for it in the book ("Stephan") are inside the new name ("Stephan Bagradian").
    Every source is judged on its own; if exactly one qualifies it is rescued and the other sources stay blocked
    (Bagradian Effendi and Young Bagradian were renamed onto the same name as Stephan Sets Out, wrongly).
    Yakov Stalin -> Stalin fails the first test (39 mentions against 67)."""
    found = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        sources = [m for m in members if m in canon and canon[m] != ents[m]["label"]]
        if not sources:
            continue
        targets = [m for m in members if m not in sources]
        rescued_source = None
        if rescue and targets and all(ents[t]["source"] is None for t in targets):
            top = max(max(ents[t]["count"] for t in targets), 1)
            ok = [m for m in sources
                  if (ents[m].get("surface") or "") and ents[m]["surface"] in canon[m].lower()
                  and ents[m]["count"] >= RESCUE_RATIO * top]
            if len(ok) == 1:
                rescued_source = ok[0]
        found.append({"key": key, "sources": sources, "targets": targets,
                      "rescued": rescued_source is not None, "rescued_source": rescued_source})
    return found


def report_collisions(ents, canon, collisions, log, limit=12):
    """Log what was rescued and what still needs a human look (no model decides these)."""
    def desc(ids):
        return ", ".join(f"{ents[m]['label']!r} ({ents[m]['count']} mentions"
                         f"{', Wikipedia' if ents[m]['source'] is not None else ''})" for m in ids)
    open_ = []
    for c in collisions:
        if c["rescued"]:
            src = c["rescued_source"]
            log(f"  rescue: {desc([src])}, shown in the book as {ents[src].get('surface')!r}, "
                f"renamed to {canon[src]!r}, absorbing {desc(c['targets'])}")
            rest = [m for m in c["sources"] if m != src]
            if rest:
                open_.append({"sources": rest, "targets": [src], "note": "the name now belongs to the rescued entity"})
        else:
            open_.append(c)
    open_.sort(key=lambda c: -ents[c["sources"][0]]["count"])
    for c in open_[:limit]:
        log(f"  needs review: {desc(c['sources'])} -> {canon[c['sources'][0]]!r} "
            + (f"collides with {desc(c['targets'])}" if c["targets"] else "(they would all get the same name)")
            + (f" ({c['note']})" if c.get("note") else "") + "; left as WordDumb made it")
    if len(open_) > limit:
        log(f"  ... and {len(open_) - limit} more renames left as WordDumb made them")


def split_into_pages(cur, eid, stages):
    """Give an entity one row per stage card. stages = [(card, from_position)], from_position None for the first.
    Row j takes the entity's occurrences from stage j's position up to stage j+1's; the first row stays the
    original entity. A row that would hold no occurrence is dropped. -> [(row id, card)]"""
    label, loc_label, typ, has_card = cur.execute(
        "SELECT label, loc_label, type, has_info_card FROM entity WHERE id = ?", (eid,)).fetchone()
    wildcard = cur.execute("SELECT source_wildcard FROM entity_description WHERE entity = ?", (eid,)).fetchone()
    wildcard = wildcard[0] if wildcard else None
    next_id = cur.execute("SELECT MAX(id) FROM entity").fetchone()[0] + 1
    rows = [(eid, stages[0][0])]
    for j in range(1, len(stages)):
        card, frm = stages[j]
        hi = stages[j + 1][1] if j + 1 < len(stages) else None
        cur.execute("INSERT INTO entity (id, label, loc_label, type, count, has_info_card) VALUES (?, ?, ?, ?, 0, ?)",
                    (next_id, label, loc_label, typ, has_card))
        cur.execute("INSERT INTO entity_description (text, source_wildcard, source, entity) VALUES (?, ?, NULL, ?)",
                    (card, wildcard, next_id))
        if hi is None:
            cur.execute("UPDATE occurrence SET entity = ? WHERE entity = ? AND start >= ?", (next_id, eid, frm))
        else:
            cur.execute("UPDATE occurrence SET entity = ? WHERE entity = ? AND start >= ? AND start < ?",
                        (next_id, eid, frm, hi))
        if cur.execute("SELECT count(*) FROM occurrence WHERE entity = ?", (next_id,)).fetchone()[0] == 0:
            cur.execute("DELETE FROM entity_description WHERE entity = ?", (next_id,))
            cur.execute("DELETE FROM entity WHERE id = ?", (next_id,))
            continue
        rows.append((next_id, card))
        next_id += 1
    for rid, _card in rows:
        cur.execute("UPDATE entity SET count = (SELECT count(*) FROM occurrence WHERE entity = ?) WHERE id = ?",
                    (rid, rid))
    return rows


def apply(xray: Path, work, ents, occ, results, log, allow_merge: bool = True, page_aware: bool = False,
          rescue: bool = True, source: Path | None = None):
    """source: the file the entities were read from (the cast pass's repaired copy); xray stays the file that is replaced."""
    tmp = xray.with_suffix(".asc.tmp")
    shutil.copy2(source or xray, tmp)
    conn = sqlite3.connect(tmp)
    cur = conn.cursor()

    by_id = {w["id"]: w for w in work}
    renames = merges = descs = blocked_renames = 0

    # 1. canonical name per entity (only where grounded and non-empty)
    canon = canonical_names(work, results)

    # 2. merge groups: same canonical name AND same type -> one survivor.
    groups = name_groups(ents, canon)

    merged_away: set[int] = set()
    # Without merging, a rename that lands on another entity's name would leave two
    # entries with one label, so those renames are dropped (descriptions still apply) -
    # unless the rescue rule shows the entity is the target under a bad label.
    keep_label: set[int] = set()
    rescued_groups: dict = {}
    if not allow_merge:
        collisions = find_collisions(ents, canon, groups, rescue)
        report_collisions(ents, canon, collisions, log)
        for c in collisions:
            if c["rescued"]:
                rescued_groups[c["key"]] = [c["rescued_source"]] + c["targets"]
                keep_label.update(m for m in c["sources"] if m != c["rescued_source"])
            else:
                keep_label.update(c["sources"])
    # A rename that only makes the name longer ("Morel" -> "E. D. Morel", "Casement" -> "Roger Casement") collides
    # with the entry that already has the long name, but it is not a conflation of two people: the entity keeps its
    # label and gets its card. Without this the main characters of a history book stayed raw (King Leopold's
    # Ghost: Morel 270 mentions, Casement 169, Conrad 93, Sanford, Sheppard, Rom ...).
    kept_label = 0
    for eid in list(keep_label):
        old, new = ents[eid]["label"], canon.get(eid)
        if new and new != old and same_name_longer(old, new):
            keep_label.discard(eid)
            canon[eid] = old
            kept_label += 1
    if kept_label:
        log(f"  {kept_label} renames that only made a name longer or re-titled it kept their label and got their card "
            f"(a collision with an entry that already has the longer name is not a conflation)")
    for (_, _typ), members in (groups.items() if allow_merge else list(rescued_groups.items())):
        if len(members) < 2:
            continue
        # Prefer a Wikipedia-described member as survivor, ahead of mention count.
        # Merging into a higher-count fallback entity silently throws away a real
        # Wikipedia description: "Protocols of the Elders of Zion" (1 mention, with
        # an article) was absorbed by "Protocols" (3 mentions, excerpt) and lost it.
        members.sort(key=lambda e: (ents[e]["source"] is None, -ents[e]["count"], e))
        survivor, losers = members[0], members[1:]
        if ents[survivor]["source"] is not None:
            # A Wikipedia entity keeps its own name and description; it only
            # absorbs the losers' occurrences and counts.
            canon.pop(survivor, None)
        elif survivor not in canon:
            # the survivor takes the canonical name even if it wasn't enriched itself
            canon[survivor] = next((canon[m] for m in members if m in canon),
                                   ents[survivor]["label"])
        for loser in losers:
            cur.execute("UPDATE occurrence SET entity = ? WHERE entity = ?", (survivor, loser))
            cur.execute("DELETE FROM entity_description WHERE entity = ?", (loser,))
            cur.execute("DELETE FROM entity WHERE id = ?", (loser,))
            ents[survivor]["count"] += ents[loser]["count"]
            merged_away.add(loser)
            merges += 1
        cur.execute("UPDATE entity SET count = ? WHERE id = ?",
                    (ents[survivor]["count"], survivor))

    # 3. rename + shift occurrence spans, and write descriptions (page-aware: one row per stage card first)
    paged = extra_rows = 0
    for eid, new_name in canon.items():
        if eid in merged_away:
            continue
        old = ents[eid]["label"]
        if eid in keep_label:
            # The rename collides with another entity's name, which is the signature of
            # a model conflating two people ("Yakov Stalin" -> "Stalin"). Its description
            # was written for the OTHER entity, so leave this one exactly as WordDumb
            # made it rather than attach the wrong person's blurb.
            blocked_renames += 1
            continue
        r = results.get(old)                 # survivors may not have been enriched
        rows = [(eid, r.description.strip() if r and r.description.strip() else None)]
        stages = getattr(r, "stages", None) or []
        if page_aware and len(stages) > 1 and all(st[1] is not None for st in stages[1:]):
            rows = split_into_pages(cur, eid, stages)
            paged += 1
            extra_rows += len(rows) - 1
        if new_name != old:
            renames += 1
        for rid, text in rows:
            if new_name != old:
                cur.execute("UPDATE entity SET label = ? WHERE id = ?", (new_name, rid))
                # keep Kindle's highlight over the right words when we trimmed the name
                off = old.find(new_name)
                if off >= 0:
                    cur.execute(
                        "UPDATE occurrence SET start = start + ?, length = ? WHERE entity = ?",
                        (off, len(new_name), rid))
            if text:
                cur.execute(
                    "UPDATE entity_description SET text = ?, source = NULL WHERE entity = ?",
                    (text, rid))
                descs += 1

    # 4. bookkeeping Kindle relies on
    people = cur.execute("SELECT count(*) FROM entity WHERE label IS NOT NULL AND type = 1").fetchone()[0]
    terms = cur.execute("SELECT count(*) FROM entity WHERE label IS NOT NULL AND type = 2").fetchone()[0]
    cur.execute("UPDATE book_metadata SET num_people = ?, num_terms = ?", (people, terms))
    for typ in (1, 2):
        top = [str(r[0]) for r in cur.execute(
            "SELECT id FROM entity WHERE label IS NOT NULL AND type = ? "
            "ORDER BY count DESC LIMIT 10", (typ,))]
        cur.execute("UPDATE type SET top_mentioned_entities = ? WHERE id = ?",
                    (",".join(top), typ))

    conn.commit()
    # integrity gate before anything replaces the real file
    bad = cur.execute(
        "SELECT count(*) FROM occurrence WHERE entity NOT IN (SELECT id FROM entity)").fetchone()[0]
    conn.execute("PRAGMA integrity_check")
    conn.close()
    if bad:
        tmp.unlink(missing_ok=True)
        raise Skip(f"integrity check failed: {bad} orphaned occurrences")

    bak = xray.with_suffix(".asc.bak")
    orig = xray.with_suffix(".asc.orig")
    if not orig.exists():
        # one-time copy of the earliest file we can find, never overwritten, so a
        # second run (or a different provider) cannot destroy WordDumb's original
        shutil.copy2(bak if bak.exists() else xray, orig)
    shutil.copy2(xray, bak)
    shutil.move(str(tmp), str(xray))
    note = "" if allow_merge else (
        f" (merging off; {blocked_renames} entries left as WordDumb made them because "
        f"the new name collided with another entity)"
        + (f"; {len(rescued_groups)} blocked renames rescued" if rescued_groups else ""))
    if page_aware:
        note += f"; page-aware: {paged} characters now have {extra_rows} extra cards for later parts of the book"
    log(f"  applied: {descs} descriptions, {renames} renames, {merges} merges{note}")
    log(f"  entities now {people} people / {terms} terms; "
        f"backups: {bak.name} (previous), {orig.name} (earliest)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xray", required=True, type=Path)
    ap.add_argument("--text", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="only the N most-mentioned (testing)")
    ap.add_argument("--provider", choices=["claude", "local"], default="claude")
    ap.add_argument("--merge", choices=["yes", "no"], default="yes",
                    help="merge entities that resolve to the same name")
    ap.add_argument("--model-high", default="", help="model for entities with many mentions")
    ap.add_argument("--model-bulk", default="", help="model for everything else")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000/v1", help="local server (OpenAI API)")
    ap.add_argument("--parallel", type=int, default=4, help="concurrent requests (local)")
    ap.add_argument("--unload-bulk", choices=["yes", "no"], default="yes",
                    help="local: unload the bulk model when finished, if this run loaded it and it is not pinned")
    ap.add_argument("--same-model-tiers", choices=["yes", "no"], default="yes",
                    help="local: when the top-tier and bulk model are the same, still give the top entities "
                         "8 excerpts and layered cards (no = one tier, 5 excerpts each, as in v1.2.6)")
    ap.add_argument("--page-aware", choices=["yes", "no"], default="no",
                    help="local, merging off (EXPERIMENT): give a character one card per stretch of the book; "
                         "the card shown depends on where the reader taps the name")
    ap.add_argument("--title", default="", help="the book's title: an entry whose Wikipedia text starts with it is the "
                    "novel's own article, not a description of a character, and is treated as undescribed")
    ap.add_argument("--wiki-check", choices=["yes", "no"], default="yes",
                    help="local, people layout: check the ties and roles against the Wikipedia pages of the book and of its "
                         "fictional characters and remove what they contradict. Wikipedia text never enters a card. Only page "
                         "titles (the book's title, the characters' names) are sent to en.wikipedia.org")
    ap.add_argument("--people-layout", choices=["yes", "no"], default="yes",
                    help="local: cards for people in the fixed layout - ties to other people first (each backed by a quote), "
                         "then role, then progress; no = the free-text card of v1.2.10")
    ap.add_argument("--rescue", choices=["yes", "no"], default="yes",
                    help="local, merging off: rename and merge an entity the model renamed onto a smaller entity's name "
                         "when it plainly is the same one under a bad label (a chapter title that tags a character)")
    ap.add_argument("--tie-verify", choices=["yes", "no"], default="yes",
                    help="local, people layout: a second request per tie asks whether the quote it rests on really says it")
    ap.add_argument("--brief", type=Path, default=None,
                    help="local: a JSON file with what the library's metadata says about the book (tags, blurb ...); it only "
                         "primes the model on what kind of book this is and is never card text")
    ap.add_argument("--ties-from-start", choices=["yes", "no"], default="yes",
                    help="local, people layout: show a backed family tie on every page of a card (yes), or only from the sentence "
                         "that first states it (no, the 1.2.11 behaviour)")
    ap.add_argument("--cast-pass", choices=["yes", "no"], default="yes",
                    help="local: before any card is written, settle who the characters are - move people WordDumb typed as "
                         "terms to the people tab, recover mentions it dropped, split a bare name shared by several "
                         "characters, drop Wikipedia texts that describe something else with the same name")
    ap.add_argument("--ipa", choices=["yes", "no"], default="no",
                    help="put a UK / US pronunciation line at the top of the entries of names a reader would stumble over "
                         "(dictionary first, the local model for the rest, marked with a tilde)")
    ap.add_argument("--ipa-dict", type=Path, default=None, help="the dictionary's headword index (JSON, built by the plugin)")
    ap.add_argument("--ipa-count", type=int, default=150, help="at most this many of the most-mentioned names")
    ap.add_argument("--checkpoint", type=Path, default=None,
                    help="local: where to write the per-stage cards (JSON, for inspection); "
                         "default ~/.claude-xray/checkpoints/<xray file>.layers.json")
    args = ap.parse_args()
    if args.checkpoint is None:
        args.checkpoint = Path.home() / ".claude-xray" / "checkpoints" / (args.xray.name + ".layers.json")

    # calibre captures stdout; entity names are not ASCII on a Russian novel
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    def log(msg):
        print(msg, flush=True)

    # machine-readable outcome for the calibre plugin: "applied" means the X-Ray
    # was actually rewritten, not merely that the job avoided crashing.
    def result(state):
        log(f"CLAUDE_XRAY_RESULT={state}")
        return 0

    if args.page_aware == "yes" and (args.provider != "local" or args.merge != "no"):
        print("  page-aware needs --provider local and --merge no; ignored")
        args.page_aware = "no"
    positions, texts = load_book_text(args.text)
    global TIES_FROM_START, BRIEF_SHORT
    TIES_FROM_START = args.ties_from_start == "yes"
    args.brief_full = ""
    args.brief_kind = ""
    if args.brief and book_brief and args.provider == "local":
        brief_ = book_brief.load(args.brief)
        if brief_:
            args.brief_full = book_brief.render(brief_, blurb=True)       # cast pass: who is in the book
            BRIEF_SHORT = book_brief.render(brief_, blurb=False)           # tie scan: what kind of book, no plot
            args.brief_kind = brief_.get("kind", "")
            print(f"  brief  : {brief_.get('title', '')} - {', '.join(brief_.get('tags', [])[:5]) or 'no tags'}; "
                  f"kind {brief_.get('kind')}; blurb {len(brief_.get('blurb', ''))} characters", flush=True)
    src = args.xray
    cast_file = args.xray.with_suffix(".asc.cast")
    if args.provider == "local" and args.cast_pass == "yes":
        try:
            import cast_pass
            rep_ = cast_pass.run(args.xray, cast_file, positions, texts, log, args, is_citation=looks_like_citation,
                                 key=load_omlx_key(), is_fictional=lambda t: bool(FICTION.search(t)))
        except Exception as exc:                      # nothing in the cast pass may fail the job
            rep_ = None
            log(f"  cast pass skipped ({exc.__class__.__name__}: {exc})")
        if rep_:
            src = cast_file
            LOCKED_LABELS.update(rep_.get("locked", []))
            for bare in rep_.get("split", {}):
                SHARED_BARE.add(" ".join(t for t in re.findall(r"[\w-]+", norm_text(bare)) if len(t) >= 3 and t not in _TITLE_WORDS))
            try:
                args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
                (args.checkpoint.parent / (args.xray.name + ".cast.json")).write_text(
                    json.dumps(rep_, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError:
                pass
        else:
            cast_file.unlink(missing_ok=True)
    try:
        return _run(args, src, cast_file, positions, texts, log, result)
    finally:
        cast_file.unlink(missing_ok=True)


def _run(args, src, cast_file, positions, texts, log, result):
    work, ents, occ = collect(src, positions, texts, page_aware=args.page_aware == "yes", title=args.title)
    if args.limit:
        work = work[: args.limit]
    log(f"  provider : {'local (' + args.base_url + ')' if args.provider == 'local' else 'Claude API'}"
        f"; merging {'on' if args.merge == 'yes' else 'off'}")
    skipped = LAST_COLLECT["citation_skipped"]
    log(f"  {len(work)} entities to enrich (Wikipedia-described left alone"
        + (f"; {skipped} footnote/index-only entries skipped" if skipped else "") + ")")
    args.wiki_pages = list(LAST_COLLECT["fictional"])      # fictional characters that have a page of their own
    args.tie_candidates = (find_tie_candidates(work, occ, positions, texts)
                           if args.provider == "local" and args.people_layout == "yes" else [])
    if LAST_COLLECT["fictional"]:
        names_ = LAST_COLLECT["fictional"]
        log(f"  {len(names_)} Wikipedia texts describe fictional characters or places and are not used (the book's own "
            f"words are used instead): {', '.join(names_[:6])}{' ...' if len(names_) > 6 else ''}")
    if LAST_COLLECT["book_article"]:
        names_ = LAST_COLLECT["book_article"]
        log(f"  {len(names_)} entries carried the book's own Wikipedia article instead of their own and are treated as "
            f"undescribed: {', '.join(names_[:6])}{' ...' if len(names_) > 6 else ''}")
    if LAST_COLLECT["duplicate_labels"]:
        log(f"  {LAST_COLLECT['duplicate_labels']} entries share a label with a more-mentioned one and are left as they are")
    if args.page_aware == "yes":
        log("  page-aware: excerpts spread over the first "
            f"{int(PAGE_SPAN * 100)}% of each entity's mentions; one card per stretch of the book")
    if not work:
        return result("skipped")

    try:
        results = enrich(work, log, args, book=norm_text(" ".join(texts)))
    except Skip as exc:
        log(f"  Enrichment skipped: {exc}")
        log("  X-Ray left exactly as generated - no changes made")
        return result("skipped")      # never fail the calibre job

    if args.dry_run:
        if args.provider == "local" and args.merge == "no":
            canon = canonical_names(work, results)
            report_collisions(ents, canon, find_collisions(ents, canon, name_groups(ents, canon), args.rescue == "yes"), log)
        for w in work[:25]:
            r = results.get(w["name"])
            if r:
                log(f"    {w['name']!r} -> {r.canonical_name!r}: {r.description}")
        return result("dryrun")

    try:
        apply(args.xray, work, ents, occ, results, log, allow_merge=args.merge == 'yes',
              page_aware=args.page_aware == 'yes', rescue=args.rescue == 'yes',
              source=src if src != args.xray else None)
    except Exception as exc:                    # nothing here may fail the job
        args.xray.with_suffix(".asc.tmp").unlink(missing_ok=True)
        log(f"  apply aborted ({exc.__class__.__name__}: {exc}) - X-Ray unchanged")
        return result("skipped")
    if args.ipa == "yes":
        add_pronunciations(args)
    return result("applied")


def add_pronunciations(args):
    """The pronunciation lines, over the finished X-Ray. Needs the local model for names the dictionary lacks; without it
    (Claude route, server down) only dictionary names get a line."""
    try:
        import ipa_pass
        client = None
        if args.provider == "local":
            import cast_pass
            client = cast_pass.Client(args.base_url, args.model_high or args.model_bulk, load_omlx_key())
            client.prefix = BRIEF_SHORT or ""
        index = ipa_pass.load_index(args.ipa_dict) if args.ipa_dict else {}
        nonfiction = bool(getattr(args, "brief_kind", "") == "nonfiction")
        ipa_pass.annotate(args.xray, lambda m: print(m, flush=True), index=index, client=client, workers=args.parallel,
                          limit=args.ipa_count, people_from_dictionary=nonfiction)
    except Exception as exc:                    # nothing here may fail the job
        print(f"  pronunciation skipped ({exc.__class__.__name__}: {exc})", flush=True)


if __name__ == "__main__":
    sys.exit(main())
