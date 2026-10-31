"""The provider chooser shown every time Enhance X-Ray is clicked.

Claude API or a local OpenAI-compatible server (oMLX). The last choice is
remembered in calibre's plugin prefs and pre-fills the dialog; no secrets are
stored there - API keys stay in the environment / macOS Keychain.
"""

import json
import os
import urllib.error
import urllib.request
from html import escape

from qt.core import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QStackedWidget,
    QTimer, QVBoxLayout, QWidget,
)

from calibre_plugins.claude_xray import enrich

DEFAULTS = {
    "provider": "claude",
    "claude_model_high": enrich.MODEL_HIGH,
    "claude_model_bulk": enrich.MODEL_BULK,
    "claude_merge": True,
    "local_url": "http://127.0.0.1:8000/v1",
    "local_model_bulk": "",
    "local_model_high": None,      # None = never chosen -> auto-pick; "" = chose none
    "local_merge": False,
    "local_unload_bulk": True,
    "local_page_aware": True,
    "local_wiki_check": True,
    "local_cast_pass": True,
    "local_brief": True,
    "local_ipa": True,
    "ipa_dictionary": "Collins English Dictionary",     # title of a dictionary in the library (MOBI/AZW); not shown in the dialog
    "dry_run": False,
}
NO_TOP_TIER = "(none - use the bulk model for everything)"


def default_top_model(models, bulk):
    """Preferred top-tier model: Qwen3.6-35B-A3B (plain build), else a Qwen3.8 6-bit, else any Qwen3.8, else none."""
    low = {m: m.lower() for m in models}
    skip = ("distill", "uncensored", "heretic", "reasoning", "abliterat")
    q36 = [m for m in models if "qwen3.6" in low[m] and "35b" in low[m] and not any(w in low[m] for w in skip) and m != bulk]
    q38 = [m for m in models if "qwen3.8" in low[m] and m != bulk]
    six = [m for m in q38 if "6bit" in low[m].replace("-", "").replace("_", "")]
    return (q36 or six or q38 or [""])[0]


def fetch_models(base_url: str, key: str | None, timeout: float = 3.0):
    """-> (model ids, error text). Never raises."""
    req = urllib.request.Request(base_url.rstrip("/") + "/models")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        # oMLX lists speculative-decoding drafters ("...-DFlash2") as models; they cannot answer requests
        return [m["id"] for m in data.get("data", []) if "dflash" not in m["id"].lower()], ""
    except urllib.error.HTTPError as exc:
        return [], f"server answered HTTP {exc.code}"
    except Exception as exc:        # refused, timeout, bad JSON...
        return [], exc.__class__.__name__


def _wrapped(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    return label


class EnhanceDialog(QDialog):
    def __init__(self, parent, prefs, book_title: str, already_enhanced: bool):
        super().__init__(parent)
        self.prefs = prefs
        self.models: list[str] = []
        self.setWindowTitle("Claude X-Ray - Enhance X-Ray")
        self.setMinimumWidth(640)
        root = QVBoxLayout(self)

        root.addWidget(_wrapped(f"<b>{escape(book_title)}</b>"))
        if already_enhanced:
            warn = _wrapped(
                "<span style='color:#c0392b'><b>Already enhanced.</b></span> This book is ticked in "
                "<i>#xray_enhanced</i>. Running again re-enhances the current X-Ray, and costs money "
                "if you choose Claude.")
            root.addWidget(warn)

        box = QGroupBox("Route the enhancement through")
        box_layout = QVBoxLayout(box)
        self.r_claude = QRadioButton("Claude API  -  best quality, billed to your Anthropic account")
        self.r_local = QRadioButton("Local model  -  free, runs on this Mac through oMLX")
        box_layout.addWidget(self.r_claude)
        box_layout.addWidget(self.r_local)
        root.addWidget(box)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._claude_page())
        self.stack.addWidget(self._local_page())
        root.addWidget(self.stack)
        self.notice = _wrapped("")                        # warnings that must stay readable (wraps, resizes)
        self.notice.setVisible(False)
        root.addWidget(self.notice)

        self.dry_run = QCheckBox("Dry run: show proposed changes in the job log, write nothing")
        self.dry_run.setChecked(bool(prefs["dry_run"]))
        root.addWidget(self.dry_run)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("Enhance")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self.r_claude.toggled.connect(self._provider_changed)
        self.r_local.toggled.connect(self._provider_changed)
        self.r_local.setChecked(prefs["provider"] == "local")
        self.r_claude.setChecked(prefs["provider"] != "local")
        self.refresh_models()
        self._provider_changed()

    # ------------------------------------------------------------- pages
    def _claude_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.claude_high = QLineEdit(self.prefs["claude_model_high"])
        self.claude_bulk = QLineEdit(self.prefs["claude_model_bulk"])
        form.addRow("Top-tier model (15+ mentions):", self.claude_high)
        form.addRow("Bulk model (the rest):", self.claude_bulk)
        self.claude_merge = QCheckBox("Merge entries that resolve to the same name")
        self.claude_merge.setChecked(bool(self.prefs["claude_merge"]))
        form.addRow(self.claude_merge)
        self.claude_status = _wrapped("")
        form.addRow(self.claude_status)
        form.addRow(_wrapped(
            "<i>Estimate for a long novel: about $1.50 and 9 minutes. "
            "The key is read from ANTHROPIC_API_KEY or the macOS Keychain.</i>"))
        return page

    def _local_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        url_row = QHBoxLayout()
        self.local_url = QLineEdit(self.prefs["local_url"])
        refresh = QPushButton("Refresh models")
        refresh.clicked.connect(self.refresh_models)
        url_row.addWidget(self.local_url, 1)
        url_row.addWidget(refresh)
        form.addRow("Server URL:", url_row)
        self.local_bulk = QComboBox()
        self.local_bulk.setEditable(True)
        self.local_high = QComboBox()
        self.local_high.setEditable(True)
        form.addRow("Bulk model:", self.local_bulk)
        form.addRow(f"Top-tier model (top {enrich.TOP_TIER_SIZE} entities):", self.local_high)
        self.local_merge = QCheckBox("Merge entries that resolve to the same name")
        self.local_merge.setChecked(bool(self.prefs["local_merge"]))
        self.local_unload = QCheckBox("Unload the bulk model when finished (frees ~20 GB)")
        self.local_unload.setChecked(bool(self.prefs["local_unload_bulk"]))
        self.local_unload.setToolTip(
            "Frees about 20 GB when the run ends. Never unloads a model that was already loaded or pinned.")
        form.addRow(self.local_unload)
        self.local_merge.setToolTip("Combines names like 'Commissar Krymov' and 'Krymov'. Keep off for local models.")
        form.addRow(self.local_merge)
        self.local_page_aware = QCheckBox("Page-aware cards")
        self.local_page_aware.setChecked(bool(self.prefs["local_page_aware"]) and not self.local_merge.isChecked())
        self.local_page_aware.setToolTip("The card changes as the book goes on, based on where you tap a name.")
        self.local_page_aware.toggled.connect(self._page_aware_toggled)
        self.local_merge.toggled.connect(self._page_aware_toggled)
        form.addRow(self.local_page_aware)
        self.local_wiki = QCheckBox("Check facts against Wikipedia")
        self.local_wiki.setChecked(bool(self.prefs["local_wiki_check"]))
        self.local_wiki.setToolTip("Removes relationships Wikipedia contradicts. Only page titles are sent to wikipedia.org.")
        form.addRow(self.local_wiki)
        self.local_cast = QCheckBox("Fix who the characters are first")
        self.local_cast.setChecked(bool(self.prefs["local_cast_pass"]))
        self.local_cast.setToolTip("Moves characters WordDumb filed as terms to People, recovers mentions it dropped, "
                                   "splits a first name shared by several characters, and drops Wikipedia texts about "
                                   "something else. Adds about a minute.")
        form.addRow(self.local_cast)
        self.local_brief = QCheckBox("Use the library's book details as context")
        self.local_brief.setChecked(bool(self.prefs["local_brief"]))
        self.local_brief.setToolTip("Tells the model the book's title, tags and blurb from calibre, so it knows what kind of book "
                                    "it is reading. Never used as card text. Nothing leaves the Mac.")
        form.addRow(self.local_brief)
        self.local_ipa = QCheckBox("Add UK / US pronunciation to names")
        self.local_ipa.setChecked(bool(self.prefs["local_ipa"]))
        self.local_ipa.setToolTip("A first line like 'UK /ɡɒnˈzɑːɡ/ · US /ɡɑːnˈzɑːɡ/' on the entries of names that are hard to say. "
                                  "Uses the Collins English Dictionary in your library where it has the name, otherwise the "
                                  "local model, whose guesses start with a tilde (≈). Ordinary English names get no line. "
                                  "Adds under a minute.")
        form.addRow(self.local_ipa)
        self._page_aware_toggled()
        self.local_status = _wrapped("")
        form.addRow(self.local_status)
        form.addRow(_wrapped(
            "<i>Estimate for a long novel: free, about 5-9 minutes (page-aware: a little longer). oMLX must be "
            "running; the first request is slower while the model loads.</i>"))
        return page

    def _page_aware_toggled(self, *_):
        """Page-aware cards need merging off: ticking one unticks and locks the other."""
        if self.local_page_aware.isChecked():
            self.local_merge.setChecked(False)
        self.local_merge.setEnabled(not self.local_page_aware.isChecked())

    # ------------------------------------------------------------- state
    def refresh_models(self):
        cur_bulk = self.local_bulk.currentText()
        cur_high = self.local_high.currentText()
        self.models, error = fetch_models(self.local_url.text().strip(), enrich.load_omlx_key())
        self.local_bulk.clear()
        self.local_high.clear()
        self.local_bulk.addItems(self.models)
        self.local_high.addItem(NO_TOP_TIER)
        self.local_high.addItems(self.models)
        if self.models:
            bulk = cur_bulk or self.prefs["local_model_bulk"] or next(
                (m for m in self.models if "gemma" in m.lower()), self.models[0])
            if cur_high:                                  # keep what is on screen on Refresh
                high = "" if cur_high == NO_TOP_TIER else cur_high
            elif self.prefs["local_model_high"] is not None:
                high = self.prefs["local_model_high"]     # saved choice ("" = none)
            else:                                         # first run: strongest Qwen3.8 if present
                high = default_top_model(self.models, bulk)
            notes = []
            if bulk not in self.models:                   # saved/typed model was removed from the server
                fallback = next((m for m in self.models if "gemma" in m.lower()), self.models[0])
                if bulk:
                    notes.append(f"bulk model '{bulk}' is no longer on the server; using '{fallback}'")
                bulk = fallback
            if high and high not in self.models:
                fallback = default_top_model(self.models, bulk)
                notes.append(f"top-tier model '{high}' is no longer on the server; "
                             f"using {repr(fallback) if fallback else 'none'}")
                high = fallback
            self.local_bulk.setCurrentText(bulk)
            self.local_high.setCurrentText(high or NO_TOP_TIER)
            self.local_status.setText(f"<span style='color:#27ae60'>&#10004; {len(self.models)} "
                                      f"model(s) available on the server</span>")
            self.notice.setText("<br>".join(
                f"<span style='color:#b9770e'>&#9888; {escape(n)}</span>" for n in notes))
        else:
            self.notice.setText("")
            self.local_status.setText(
                f"<span style='color:#c0392b'>&#10008; Server not reachable ({escape(error)}). "
                f"Start oMLX from the menu bar, then press Refresh.</span>")
        self._provider_changed()

    def _claude_key_ok(self) -> bool:
        return bool(enrich.load_api_key() or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def _provider_changed(self, *_):
        local = self.r_local.isChecked()
        self.stack.setCurrentIndex(1 if local else 0)
        self.notice.setVisible(local and bool(self.notice.text()))
        self._fit()
        key_ok = self._claude_key_ok()
        self.claude_status.setText(
            "<span style='color:#27ae60'>&#10004; API key found</span>" if key_ok else
            "<span style='color:#c0392b'>&#10008; No API key found. Add one to the macOS "
            "Keychain (service 'claude-xray') or set ANTHROPIC_API_KEY.</span>")
        self.ok_button.setEnabled(bool(self.models) if local else key_ok)

    def _fit(self):
        """Grow to the height the wrapped labels need at the current width (Qt does not do this
        for labels whose text changes after the dialog was laid out)."""
        lay = self.layout()
        lay.invalidate()
        lay.activate()
        need = lay.totalHeightForWidth(self.width())
        if need > self.height():
            self.resize(self.width(), need)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._fit)

    # ----------------------------------------------------------- result
    def choices(self) -> dict:
        high = self.local_high.currentText().strip()
        return {
            "provider": "local" if self.r_local.isChecked() else "claude",
            "claude_model_high": self.claude_high.text().strip(),
            "claude_model_bulk": self.claude_bulk.text().strip(),
            "claude_merge": self.claude_merge.isChecked(),
            "local_url": self.local_url.text().strip(),
            "local_model_bulk": self.local_bulk.currentText().strip(),
            "local_model_high": "" if high == NO_TOP_TIER else high,
            "local_merge": self.local_merge.isChecked(),
            "local_unload_bulk": self.local_unload.isChecked(),
            "local_page_aware": self.local_page_aware.isChecked() and not self.local_merge.isChecked(),
            "local_wiki_check": self.local_wiki.isChecked(),
            "local_cast_pass": self.local_cast.isChecked(),
            "local_brief": self.local_brief.isChecked(),
            "local_ipa": self.local_ipa.isChecked(),
            "ipa_dictionary": str(self.prefs["ipa_dictionary"] or ""),
            "dry_run": self.dry_run.isChecked(),
        }

    def accept(self):
        for key, value in self.choices().items():
            self.prefs[key] = value
        super().accept()
