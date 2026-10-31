# Enhance X-Ray

A calibre plugin (internal name **Claude X-Ray**, toolbar button **Enhance X-Ray**) that improves the
X-Ray files WordDumb makes for Kindle books. It fixes who the characters are, rewrites entity names and
cards, adds relationships and UK/US pronunciation lines. It runs on the Claude API or on a local model
(Qwen3.6 through oMLX), chosen in a dialog. If the model is unreachable it leaves the X-Ray untouched.

Current version: **1.2.15** (Mac). Platform: macOS, Apple Silicon, calibre 9.x with KFX Input/Output and the
patched WordDumb 3.36.x.

## Layout

| Path | What |
|---|---|
| `__init__.py`, `ui.py`, `dialog.py` | plugin entry, toolbar action, provider dialog |
| `enrich.py` | the worker (cards, ties, Wikipedia check, backups, integrity gate) |
| `cast_pass.py`, `kin_words.py`, `book_brief.py`, `ipa_pass.py` | cast pass, kin words, library-metadata brief, pronunciation lines |
| `tests/` | five suites, standard library only, no oMLX needed |
| `tools/` | dev scripts (cast pass alone, pronunciation pass alone, dialog smoke test, end-to-end run) |

## Build and install

```bash
./build.sh                      # -> dist/ClaudeXRay_mac_v1.2.15.zip
# quit calibre first
/Applications/calibre.app/Contents/MacOS/calibre-customize -a dist/ClaudeXRay_mac_v1.2.15.zip
```

The Claude path also needs a venv with the `anthropic` package and a key in the macOS Keychain
(service `claude-xray`); the local path needs oMLX running and uses the standard library only.

## Tests

```bash
for t in test_local_provider test_cast_pass test_brief test_wikibare test_ipa; do python3 tests/$t.py; done
```

Each should end with `ALL PASSED` (`test_ipa` prints `FAILED: 0`).

## Notes

- X-Ray databases (`*.asc`), backups and run checkpoints are book data and are git-ignored.
- No API keys are stored in the repo. The Claude key lives in the macOS Keychain (service `claude-xray`).
