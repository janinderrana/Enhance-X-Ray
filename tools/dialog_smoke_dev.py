"""Dialog + worker-argument smoke test against this source tree (no install): calibre-debug tools/dialog_smoke_dev.py"""
import sys, types
from pathlib import Path
dev = str(Path(__file__).resolve().parent.parent)
sys.modules["calibre_plugins"] = types.ModuleType("calibre_plugins")
pkg = types.ModuleType("calibre_plugins.claude_xray"); pkg.__path__ = [dev]
sys.modules["calibre_plugins.claude_xray"] = pkg
sys.path.insert(0, dev)
from qt.core import QApplication
app = QApplication.instance() or QApplication([])
from calibre_plugins.claude_xray.dialog import EnhanceDialog, DEFAULTS
from calibre_plugins.claude_xray import ui
prefs = dict(DEFAULTS); prefs["provider"] = "local"
class P(dict):
    defaults = DEFAULTS
d = EnhanceDialog(None, P(prefs), "Test Book", False)
ch = d.choices(); ch["book_title"] = "Test Book"
print("checkbox:", d.local_ipa.text(), "| ticked:", ch["local_ipa"], "| dictionary setting:", ch["ipa_dictionary"])
print("worker args:", " ".join(ui.build_worker_args(ch)))
d.local_ipa.setChecked(False); ch = d.choices(); ch["book_title"] = "x"
print("unticked ->", ui.build_worker_args(ch)[ui.build_worker_args(ch).index("--ipa"):][:2])
# the dictionary index is built from a real MOBI by calibre's reader, once, and found current afterwards
import tempfile
import os
book = Path(os.environ["COLLINS_MOBI"])  # path to the Collins English Dictionary MOBI in your calibre library
from calibre_plugins.claude_xray import ipa_pass
out = Path(tempfile.mkdtemp()) / "ipa_dictionary.json"
logs = []
import time
t = time.time(); r1 = ui.ensure_ipa_index(book, logs.append); t1 = time.time() - t
t = time.time(); r2 = ui.ensure_ipa_index(book, logs.append); t2 = time.time() - t
print("index:", r1, f"first {t1:.1f}s, second {t2:.2f}s", "| logs:", logs)
class FakeDB:
    def search(self, q): return [7] if "Collins" in q else []
    def formats(self, i): return ("EPUB", "MOBI")
    def format_abspath(self, i, f): return f"/lib/{f}"
print("find:", ui.find_dictionary(FakeDB(), "Collins English Dictionary"), ui.find_dictionary(FakeDB(), "Oxford"), ui.find_dictionary(FakeDB(), ""))
