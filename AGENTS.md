# Agent notes

Read this before changing the repo. The README is the map of the product. This file is how to work in it without breaking the user's machine or `main`.

## Git

`main` rejects a direct push. Changes land through a pull request: one review, and `CODEOWNERS` is `* @sachafaust`. A branch created from `origin/main` tracks `main`, so a plain `git push` aims at `main`. Push with `git push -u origin HEAD:<branch>`. Do not force-push. Do not merge unless asked. When asked, `gh pr merge` reports the PR blocked; merge with `gh api -X PUT repos/sachafaust/linkurio-less/pulls/N/merge -f merge_method=merge`.

Commit as Tinker Lab `<10822166+sachafaust@users.noreply.github.com>` by setting `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, and `GIT_COMMITTER_EMAIL` for that command. Do not change git config.

If the working tree is on another branch with uncommitted work, use a second worktree. Do not pop a stash onto a different branch. Do not merge the GraphQL branch until asked.

## Run

Use this checkout's `.venv/bin/python` when it exists. Otherwise use `/Users/shey/code/linkurio-less/.venv/bin/python`. The Homebrew interpreter does not have the Neo4j driver.

`data/cartography_styles.json` is read once at process start. Static files are read on each request. A style change needs a restart. A JS or CSS change does not.

The README default port is 8744. If it is taken, pick another port. Do not kill a server you did not start.

## Data and secrets

`.env`, `gx.local.json`, `gx.schema.json`, `tests/screenshots/`, and `tests/results.json` stay uncommitted. A worktree has no `.env`; point `GX_ENV` at one. Do not print the Neo4j password. The schema cache key is the bolt address plus the database name, with any userinfo stripped.

Do not run `run_local.py --reset`. It deletes the Neo4j volume. On Docker, `--test` reloads the fixture snapshot and wipes the database first. When `.env` points at your own Neo4j, `--test` refuses unless `--load-data`, and that flag wipes a local database. Prefer `tests/run_ui_tests.py` with the venv and `GX_ENV`. It starts its own server on a free port and does not wipe Neo4j.

The UI suite targets the fixture snapshot (`data/cartography-fixtures.json.gz`): search `AWSID`, and use account `000000000000` where a hub is required. `acme-prod` exists only in the synthetic generator (`--synthetic` / `--both`). `docs/screenshots/` is the curated set. The suite writes `tests/screenshots/`.

## Data model

The data model for a Neo4j connection is cached in `gx.schema.json`. Refresh it from the main menu, the command palette, or the data model window.
