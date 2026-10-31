"""Pronunciation pass alone on a copy of an X-Ray file: python3 tools/run_ipa.py XRAY.asc [--no-dict] [--no-model] [--limit N]"""
import argparse, shutil, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cast_pass, enrich, ipa_pass
ap = argparse.ArgumentParser()
ap.add_argument("xray"); ap.add_argument("--no-dict", action="store_true"); ap.add_argument("--no-model", action="store_true")
ap.add_argument("--limit", type=int, default=ipa_pass.MAX_CANDIDATES); ap.add_argument("--model", default="Qwen3.6-35B-A3B-oQ4e-mtp")
ap.add_argument("--nonfiction", action="store_true"); ap.add_argument("--dict", default=str(Path.home() / ".claude-xray/cache/collins_ipa.json"))
a = ap.parse_args()
work = Path(__file__).resolve().parents[2] / "scratch" / "wdtest" / "ipa"
work.mkdir(parents=True, exist_ok=True)
dest = work / (Path(a.xray).name + ".ipa")
shutil.copy2(a.xray, dest)
client = None if a.no_model else cast_pass.Client("http://127.0.0.1:8000/v1", a.model, enrich.load_omlx_key(), timeout=300)
index = {} if a.no_dict else ipa_pass.load_index(a.dict)
t = time.time()
st = ipa_pass.annotate(dest, print, index=index, client=client, limit=a.limit, people_from_dictionary=a.nonfiction)
print(f"{time.time() - t:.0f}s, model calls {client.calls if client else 0}", st)
cur = sqlite3.connect(dest).cursor()
seen = set()
for label, text in cur.execute("SELECT e.label, d.text FROM entity e JOIN entity_description d ON d.entity = e.id ORDER BY e.count DESC"):
    if ipa_pass.LINE_RE.match(text or "") and label not in seen:
        seen.add(label); print(f"{label[:28]:28} {text.splitlines()[0]}")
