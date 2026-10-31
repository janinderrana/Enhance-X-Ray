# Contributing

Thanks for helping. Only the maintainer ([@janinderrana](https://github.com/janinderrana)) can approve and merge
pull requests.

## How to send a change

1. Fork the repo and create a branch (`git checkout -b my-change`).
2. Keep the change small and focused, and say in the pull request what it fixes and how you checked it.
3. Run the tests (standard library only, no model server needed). Each suite should end with `ALL PASSED`
   (`test_ipa` prints `FAILED: 0`):
   ```bash
   for t in test_local_provider test_cast_pass test_brief test_wikibare test_ipa; do python3 tests/$t.py; done
   ```
4. Open a pull request against `main`.

## Please do not commit

- API keys, tokens or passwords of any kind. Keys belong in the macOS Keychain or the environment.
- X-Ray files (`*.asc`, backups, checkpoints) or book text. They are book data and are git-ignored.
- Local paths, hostnames or other details of your own machine.

## Review

Every change is read in full before it is merged. The scripts and tests run code on the maintainer's machine, so
a pull request that changes `deploy.sh`, `tools/`, the tests or build files gets extra scrutiny and may be asked to
explain why.

## License

By contributing you agree that your work is licensed under the GNU General Public License v3.0 or later, the same as
the rest of the project (see `LICENSE`).
