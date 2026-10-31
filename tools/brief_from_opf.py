"""Build a brief JSON from a book folder's metadata.opf (for tests outside calibre). usage: brief_from_opf.py <book folder> <out json>"""
import re, sys, html
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import book_brief
s = (Path(sys.argv[1]) / "metadata.opf").read_text(encoding="utf-8")
g = lambda tag: (re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", s, re.S) or [None, ""])[1]
desc = html.unescape(g("dc:description"))
series = (re.findall(r'calibre:series"[^>]*content="([^"]*)"', s) or [""])[0]
b = book_brief.make_brief(html.unescape(g("dc:title")), [html.unescape(g("dc:creator"))], g("dc:date"), html.unescape(series), html.unescape(g("dc:publisher")),
                          [g("dc:language")], [html.unescape(t) for t in re.findall(r"<dc:subject>([^<]*)<", s)], desc)
book_brief.dump(b, sys.argv[2]); print(b.get("kind"), b.get("tags"), len(b.get("blurb", "")))
