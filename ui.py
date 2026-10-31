"""Toolbar action: enhance the selected book's X-Ray with Claude.

A dialog lets you route the work through the Claude API or a local model served
by oMLX. It runs as a calibre background job; the heavy lifting happens in an
external Python (calibre's bundled Python has no `anthropic` SDK), exactly the
way WordDumb shells out to its own interpreter.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from calibre.gui2 import error_dialog
from calibre.gui2.actions import InterfaceAction
from calibre.gui2.threaded_jobs import ThreadedJob
from calibre.utils.config import JSONConfig, config_dir

RESULT_MARKER = "CLAUDE_XRAY_RESULT="
TRACKING_COLUMN = "#xray_enhanced"   # calibre custom column, ticked on a real run


def plugin_zip(action) -> str:
    """Path of the zip calibre actually loaded.

    calibre names the installed file after the plugin's `name` ("Claude X-Ray.zip"),
    not after whatever the source zip was called - hard-coding a filename here
    silently runs a stale copy after an update.
    """
    path = getattr(action.interface_action_base_plugin, "plugin_path", None)
    if path and Path(path).is_file():
        return str(path)
    for candidate in ("Claude X-Ray.zip", "ClaudeXRay.zip"):
        found = Path(config_dir) / "plugins" / candidate
        if found.is_file():
            return str(found)
    raise RuntimeError("cannot locate the Claude X-Ray plugin zip")


def find_python() -> str:
    """A dedicated venv with `anthropic` if present (macOS/Linux), else the
    interpreter WordDumb is already configured with."""
    venv_python = Path.home() / ".claude-xray" / "venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    try:
        path = JSONConfig("plugins/worddumb").get("python_path", "")
        if path and Path(path).is_file():
            return path
    except Exception:
        pass
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "No Python interpreter found. Set one in WordDumb's settings, or put "
        "python on PATH. It needs the 'anthropic' package installed."
    )


def awake_prefix() -> list:
    """Command prefix that stops macOS idle-sleeping while the worker runs.

    A long local run (15-25 min) is exactly when nobody is at the keyboard; with the
    display off the Mac mini sleeps after one minute and the job stalls until it wakes.
    `caffeinate -i` holds an idle-sleep assertion for the lifetime of the command it wraps
    and passes the command's output and exit code straight through.
    """
    if sys.platform == "darwin" and Path("/usr/bin/caffeinate").is_file():
        return ["/usr/bin/caffeinate", "-i"]
    return []


def build_worker_args(choices: dict) -> list:
    """Dialog choices -> worker command-line arguments."""
    local = choices["provider"] == "local"
    prefix = "local" if local else "claude"
    args = ["--provider", choices["provider"],
            "--merge", "yes" if choices[f"{prefix}_merge"] else "no"]
    if choices.get("book_title"):
        args += ["--title", choices["book_title"]]
    if local:
        args += ["--base-url", choices["local_url"], "--model-bulk", choices["local_model_bulk"]]
        args += ["--unload-bulk", "yes" if choices.get("local_unload_bulk", True) else "no"]
        if choices["local_model_high"]:
            args += ["--model-high", choices["local_model_high"]]
        args += ["--wiki-check", "yes" if choices.get("local_wiki_check", True) else "no"]
        args += ["--cast-pass", "yes" if choices.get("local_cast_pass", True) else "no"]
        args += ["--ipa", "yes" if choices.get("local_ipa", True) else "no"]
        if choices.get("local_page_aware") and not choices["local_merge"]:
            args += ["--page-aware", "yes"]
    else:
        args += ["--model-high", choices["claude_model_high"],
                 "--model-bulk", choices["claude_model_bulk"]]
    return args


IPA_FORMATS = ("MOBI", "AZW", "PRC")        # what calibre's MOBI reader can open


def find_dictionary(db, title: str):
    """Path of the dictionary book in the library (the title is a setting, 'Collins English Dictionary' by default), or None"""
    if not title:
        return None
    try:
        for book_id in db.search(f'title:"{title}"'):
            fmts = {f.upper() for f in (db.formats(book_id) or ())}
            for fmt in IPA_FORMATS:
                if fmt in fmts:
                    return db.format_abspath(book_id, fmt)
    except Exception:
        pass
    return None


def ensure_ipa_index(book, log):
    """The dictionary's headword index (JSON beside the other caches), built once, rebuilt if the book file changes. None on failure."""
    out = Path.home() / ".claude-xray" / "cache" / "ipa_dictionary.json"
    try:
        from calibre_plugins.claude_xray import ipa_pass
        book = Path(book)
        if not ipa_pass.index_is_current(book, out):
            log(f"  pronunciation: reading {book.name} once to index its names ...")
            log(f"  pronunciation: {ipa_pass.build_index(book, out):,} names indexed")
        return out
    except Exception as exc:                       # the model alone can still do the names
        log(f"  pronunciation: dictionary not used ({exc.__class__.__name__}: {exc})")
        return None


def extract_book_text(book_path: Path, out: Path) -> int:
    """Dump the book's text entries the same way WordDumb does."""
    if book_path.suffix.lower() != ".kfx":
        raise RuntimeError(f"only KFX is supported for now (got {book_path.suffix})")
    from calibre_plugins.kfx_input.kfxlib import YJ_Book

    data = json.loads(YJ_Book(str(book_path)).convert_to_json_content())["data"]
    entries = [e for e in data if e["type"] == 1]
    out.write_text(json.dumps(entries), encoding="utf-8")
    return sum(len(e["content"]) for e in entries)


class ClaudeXRayAction(InterfaceAction):
    name = "Claude X-Ray"
    action_spec = (
        "Enhance X-Ray",
        None,
        "Rewrite X-Ray names and descriptions with Claude",
        None,
    )

    def genesis(self):
        self.qaction.triggered.connect(self.start)

    # ---------------------------------------------------------------- start
    def start(self):
        rows = self.gui.library_view.selectionModel().selectedRows()
        if not rows:
            return error_dialog(self.gui, "Claude X-Ray", "Select a book first.", show=True)
        book_id = self.gui.library_view.model().id(rows[0])
        db = self.gui.current_db.new_api

        fmts = {f.upper() for f in (db.formats(book_id) or ())}
        if "KFX" not in fmts:
            return error_dialog(
                self.gui, "Claude X-Ray",
                "This book has no KFX format. Only KFX is supported for now.", show=True)
        book_path = Path(db.format_abspath(book_id, "KFX"))

        xrays = sorted(book_path.parent.glob("XRAY.entities.*.asc"))
        if not xrays:
            return error_dialog(
                self.gui, "Claude X-Ray",
                "No X-Ray file found beside this book. Generate it with WordDumb first.",
                show=True)

        title = db.field_for("title", book_id)
        already = False
        try:
            already = bool(TRACKING_COLUMN in db.field_metadata
                           and db.field_for(TRACKING_COLUMN, book_id))
        except Exception:
            pass

        from calibre_plugins.claude_xray.dialog import DEFAULTS, EnhanceDialog
        prefs = JSONConfig("plugins/claude_xray")
        prefs.defaults.update(DEFAULTS)
        dlg = EnhanceDialog(self.gui, prefs, str(title), already)
        if not dlg.exec():
            return
        choices = dlg.choices()
        choices["book_title"] = str(title)
        choices["book_brief"] = self.read_brief(db, book_id) if choices.get("local_brief", True) else {}
        choices["ipa_dict_src"] = (find_dictionary(db, choices.get("ipa_dictionary", ""))
                                   if choices["provider"] == "local" and choices.get("local_ipa", True) else None)

        job = ThreadedJob(
            "claude_xray",
            f"Enhancing X-Ray for {title}",
            self.run, (book_id, book_path, xrays[0], choices), {}, self.done,
            killable=False,
        )
        self.gui.job_manager.run_threaded_job(job)
        self.gui.status_bar.show_message(f"Enhancing X-Ray for {title}", 3000)

    @staticmethod
    def read_brief(db, book_id: int) -> dict:
        """What the library's metadata says about the book, read now (so a metadata step earlier in the workflow counts)."""
        try:
            from calibre_plugins.claude_xray.book_brief import make_brief
            return make_brief(
                title=db.field_for("title", book_id) or "", authors=db.field_for("authors", book_id) or (),
                pubdate=db.field_for("pubdate", book_id), series=db.field_for("series", book_id) or "",
                publisher=db.field_for("publisher", book_id) or "", languages=db.field_for("languages", book_id) or (),
                tags=db.field_for("tags", book_id) or (), comments=db.field_for("comments", book_id) or "")
        except Exception:                                 # the brief is a help, never a reason to fail the job
            return {}

    # ------------------------------------------------------------------ job
    def run(self, book_id: int, book_path: Path, xray: Path, choices: dict,
            log=None, abort=None, notifications=None):
        log("Claude X-Ray")
        log(f"  book : {book_path.name}")
        log(f"  xray : {xray.name}")
        enhanced = False

        text_json = xray.parent / "claude_xray_text.json"
        try:
            chars = extract_book_text(book_path, text_json)
            log(f"  extracted {chars:,} characters of book text")

            python = find_python()
            zip_path = plugin_zip(self)
            log(f"  worker : {python}")
            log(f"  plugin : {zip_path}")
            awake = awake_prefix()
            cmd = awake + [python, "-I", zip_path, "--xray", str(xray), "--text", str(text_json)]
            cmd += build_worker_args(choices)
            if choices.get("book_brief"):
                from calibre_plugins.claude_xray.book_brief import dump
                brief_path = xray.parent / "claude_xray_brief.json"
                dump(choices["book_brief"], brief_path)
                cmd += ["--brief", str(brief_path)]
            if choices["provider"] == "local" and choices.get("local_ipa", True):
                if choices.get("ipa_dict_src"):
                    index = ensure_ipa_index(choices["ipa_dict_src"], log)
                    if index:
                        cmd += ["--ipa-dict", str(index)]
                else:
                    log(f"  pronunciation: no dictionary called {choices.get('ipa_dictionary')!r} (MOBI/AZW) in the library; "
                        "the local model does all the names")
            if choices.get("dry_run"):
                cmd.append("--dry-run")
            log("  route  : " + ("local model" if choices["provider"] == "local" else "Claude API"))
            if awake:
                log("  awake  : Mac kept awake while this runs (caffeinate)")

            # stream the worker's output so a long local run shows progress live
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith(RESULT_MARKER):
                    enhanced = line.split("=", 1)[1].strip() == "applied"
                    continue
                log(line)
            proc.wait()
            if proc.returncode != 0:
                # never fail the job - the X-Ray is still valid as generated
                log(f"  worker exited {proc.returncode}; X-Ray left unchanged")
        finally:
            text_json.unlink(missing_ok=True)

        return {"book_id": book_id, "enhanced": enhanced}

    # --------------------------------------------------------------- finished
    def done(self, job):
        if job.failed:
            return self.gui.job_exception(job, dialog_title="Claude X-Ray failed")
        try:
            outcome = job.result or {}
            if not outcome.get("enhanced"):
                self.gui.status_bar.show_message(
                    "Claude X-Ray: no changes made (see job log)", 6000)
                return
            note = self.mark_enhanced(outcome["book_id"])
            self.gui.status_bar.show_message(f"Claude X-Ray finished - {note}", 6000)
        except Exception:
            # the X-Ray is already written; bookkeeping must never undo that
            self.gui.status_bar.show_message("Claude X-Ray finished", 5000)

    def mark_enhanced(self, book_id: int) -> str:
        """Tick the tracking column so a re-run isn't paid for twice.

        Never raises. The enhancement is already on disk by the time this runs,
        so a missing or misconfigured column is a note in the status bar, not a
        failed job.
        """
        try:
            db = self.gui.current_db.new_api
            if TRACKING_COLUMN not in db.field_metadata:
                return (f"{TRACKING_COLUMN} not set up (optional) - add a custom "
                        f"column with lookup name "
                        f"'{TRACKING_COLUMN.lstrip('#')}' to track enhanced books")
            # a bool column renders True as "Yes"; a text column wants the word
            datatype = db.field_metadata[TRACKING_COLUMN].get("datatype")
            db.set_field(TRACKING_COLUMN,
                         {book_id: True if datatype == "bool" else "Yes"})
            self.gui.library_view.model().refresh_ids([book_id])
            return f"{TRACKING_COLUMN} set"
        except Exception as exc:
            return f"could not set {TRACKING_COLUMN} ({exc.__class__.__name__})"
