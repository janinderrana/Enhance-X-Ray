import sys
from calibre.customize.ui import initialize_plugins
initialize_plugins()
from qt.core import QApplication
app = QApplication.instance() or QApplication([])
from calibre_plugins.claude_xray.dialog import EnhanceDialog, DEFAULTS
from calibre_plugins.claude_xray.ui import build_worker_args
prefs = dict(DEFAULTS); prefs["provider"] = "local"
class P(dict):
    defaults = DEFAULTS
d = EnhanceDialog(None, P(prefs), "Test Book", False)
ch = d.choices()
print("checkboxes:", d.local_cast.text(), "|", d.local_brief.text(), "| choice:", ch["local_cast_pass"], ch["local_brief"])
ch["book_title"] = "Test Book"
print("worker args:", " ".join(build_worker_args(ch)))
d.local_cast.setChecked(False); ch = d.choices(); ch["book_title"] = "x"
print("unticked ->", [a for a in build_worker_args(ch) if a in ("--cast-pass", "yes", "no")][-2:])
