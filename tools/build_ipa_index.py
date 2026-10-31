"""Build the Collins headword index (needs calibre): /Applications/calibre.app/Contents/MacOS/calibre-debug tools/build_ipa_index.py -- BOOK OUT.json"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ipa_pass
book, out = Path(sys.argv[-2]), Path(sys.argv[-1])
t = time.time()
print("entries:", ipa_pass.build_index(book, out), f"in {time.time() - t:.1f}s", "->", out, out.stat().st_size, "bytes")
