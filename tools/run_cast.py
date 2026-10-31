"""Run only the cast pass on a scratch copy and report. usage: run_cast.py <asc> <text json> <out asc>"""
import sys, time, json, sqlite3
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import enrich, cast_pass
asc, txt, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
pos, texts = enrich.load_book_text(txt)
opts = SimpleNamespace(base_url="http://127.0.0.1:8000/v1", model_high="Qwen3.6-35B-A3B-oQ4e-mtp", model_bulk="Qwen3.6-35B-A3B-oQ4e-mtp", parallel=4, brief_full=(__import__("book_brief").render(__import__("book_brief").load(__import__("os").environ["BRIEF"]), blurb=True) if __import__("os").environ.get("BRIEF") else ""), cast_only=(sys.argv[4] if len(sys.argv) > 4 else None))
t = time.time()
rep = cast_pass.run(asc, out, pos, texts, lambda m="": print(m, flush=True), opts, is_citation=enrich.looks_like_citation, key=enrich.load_omlx_key(), is_fictional=lambda t: bool(enrich.FICTION.search(t)))
print("total seconds", round(time.time() - t, 1))
json.dump(rep, open(str(out) + ".report.json", "w"), ensure_ascii=False, indent=1)
