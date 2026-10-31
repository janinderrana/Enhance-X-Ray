"""The book brief (v1.2.13): what the library's own metadata says about the book, in a few lines for the local model.

It only tells the model what kind of book it is reading and who is in it. It is never card text (blurbs spoil the plot), never
evidence for a tie, and nothing is fetched: the fields come from calibre's database when the user clicks Enhance X-Ray, so a
metadata step earlier in the user's workflow is already in them.
"""
import html
import json
import re
from pathlib import Path

MAX_TAGS = 8
BLURB_CHARS = 700
_NONFICTION = {"history", "biography", "autobiography", "memoir", "memoirs", "politics", "political science", "science", "essays",
               "essay", "nonfiction", "non-fiction", "philosophy", "economics", "journalism", "religion", "sociology",
               "anthropology", "travel", "true crime", "reference", "criticism", "current affairs", "military history",
               "world history", "biography & autobiography", "psychology", "business"}
_FICTION = {"fiction", "novel", "novels", "fantasy", "science fiction", "mystery", "thriller", "romance", "horror", "literary fiction",
            "short stories", "adventure", "crime", "detective", "historical fiction", "magical realism", "dystopian", "saga"}
_NOISE = {"kindle", "ebook", "ebooks", "unread", "read", "to-read", "owned", "favorites", "audiobook", "own", "library"}
_BLURB_FICTION = re.compile(r"\b(?:novel|novella|a story of|saga|epic|trilogy|his debut)\b", re.I)
_BLURB_NONFICTION = re.compile(r"\b(?:biography|memoir|a history of|history of|account of|study of|investigation|journalist(?:ic)?|"
                               r"the story of how|nonfiction|non-fiction)\b", re.I)


def clean_text(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return " ".join(s.split())


def make_brief(title="", authors=(), pubdate=None, series="", publisher="", languages=(), tags=(), comments="") -> dict:
    """calibre fields -> a plain dict (empty when there is nothing useful)"""
    tags_ = []
    for t in tags or ():
        t = " ".join(str(t).split())
        if t and t.lower() not in _NOISE and len(t) <= 40 and t not in tags_:
            tags_.append(t)
    year = getattr(pubdate, "year", None)
    if not year and isinstance(pubdate, str) and re.match(r"\d{4}", pubdate):
        year = int(pubdate[:4])
    if year and year < 1000:
        year = None
    blurb = clean_text(comments)
    if len(blurb) > BLURB_CHARS:
        blurb = blurb[:BLURB_CHARS].rsplit(" ", 1)[0] + " ..."
    brief = {"title": clean_text(title), "authors": [clean_text(a) for a in (authors or ())][:3], "year": year,
             "series": clean_text(series), "publisher": clean_text(publisher), "languages": list(languages or ())[:2],
             "tags": tags_[:MAX_TAGS], "blurb": blurb}
    brief["kind"] = kind_of(brief)
    return brief if (brief["tags"] or brief["blurb"] or brief["series"]) else {}


def kind_of(brief: dict) -> str:
    """fiction / nonfiction / unknown. Tags first ("History, Politics, Biography"), then the blurb ("the seminal novel of ...");
    conflicting evidence, or "Historical" alone, stays unknown."""
    tags = {t.lower() for t in brief.get("tags", [])}
    nf, fic = bool(tags & _NONFICTION), bool(tags & _FICTION)
    if nf and not fic:
        return "nonfiction"
    if fic and not nf:
        return "fiction"
    if nf and fic:
        return "unknown"
    blurb = brief.get("blurb", "")
    b_fic, b_nf = bool(_BLURB_FICTION.search(blurb)), bool(_BLURB_NONFICTION.search(blurb))
    if b_fic and not b_nf:
        return "fiction"
    if b_nf and not b_fic:
        return "nonfiction"
    return "unknown"


def render(brief: dict, blurb: bool = True) -> str:
    """the block put in front of a prompt; "" when there is no brief"""
    if not brief:
        return ""
    bits = []
    head = brief.get("title", "")
    if brief.get("authors"):
        head += " by " + ", ".join(brief["authors"])
    if brief.get("year"):
        head += f" ({brief['year']})"
    if head:
        bits.append(head)
    if brief.get("series"):
        bits.append("Series: " + brief["series"])
    if brief.get("tags"):
        bits.append("Library tags: " + ", ".join(brief["tags"]))
    if brief.get("kind") and brief["kind"] != "unknown":
        bits.append("Kind of book: " + brief["kind"])
    if blurb and brief.get("blurb"):
        bits.append("Publisher's blurb: " + brief["blurb"])
    return ("About this book (from the library catalogue; it may be incomplete or wrong). It only tells you what kind of book "
            "this is and who is in it; every statement you write must still come from the passages shown, never from this "
            "note.\n" + "\n".join(bits) + "\n\n")


def load(path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def dump(brief: dict, path) -> None:
    Path(path).write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
