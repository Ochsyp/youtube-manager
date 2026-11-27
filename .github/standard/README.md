# The repository standard

This folder, the hooks in `.githooks/`, the workflows, the pull request template and the blocks
between `standard:begin` and `standard:end` markers are copies from repo-standard. Never edit
them here: `standard-check` fails on any byte, mode or block that differs from the `MANIFEST`,
and the next sync would undo the change. Change the standard in repo-standard, release it, and
sync.

- `MANIFEST` names the release the copies come from, and the blob id and mode of each copy and
  block.
- `bin/` holds the tools that the hooks, CI and the release workflow run.
- `tests/` holds their bats suites. `tests/lib.bash` is also the base of this repository's own
  `tests/helpers.bash`.
- `actions.allow` lists the actions the standard's workflows may use.

## Sync

`tools/sync` of repo-standard, run from a clone at a published release, writes that release's
copies and blocks into this repository on a branch `chore/standard-X.Y.Z` and prints the pull
request text. That pull request changes nothing else. A first adoption runs `tools/sync --init`;
repo-standard's `CONTRIBUTING.md`, Adopting a repository, lists every step and the GitHub
settings.

## Test

```sh
.github/standard/bin/install-tools bats
bats .github/standard/tests/
```

`install-tools` installs the pinned bats and prints its folder. In Git Bash on Windows, run the
suites one file at a time, and leave `lint.bats` to Linux. CI runs them in the job `standard`.

## Rules

The rules that the copies hold this repository to. Tools and their error messages point here.

### The config

`.github/standard.conf` is data, and only `bin/conf` reads it; nothing sources it. Each line is
`KEY=value`, `#` starts a comment line, and a key appears once unless it repeats by design
(`PRIVACY_ALLOW`, `PRIVACY_HISTORY_ACCEPT`, `VERSION_COPY`). `templates/standard.conf` in
repo-standard lists every key with its default; without a `PRIVACY_RULES` line the `base` pack
runs. `RELEASE_KIND=image` needs `IMAGE_PLATFORMS`, and `SEAL=yes` needs `VISIBILITY=public`. The
hooks read the config from `HEAD`, so a change to it takes effect once it is committed.

### standard-check, rules 1 to 10

1. The config parses. On a pull request or a push, `VISIBILITY` agrees with GitHub.
2. to 4. The copies and blocks: every copy and block of the profiles the config selects equals
   the `MANIFEST`, byte for byte and with its mode. A block sits once, in order, between its
   markers. No other file sits in the standard's folders, and a block the config does not select
   is absent.
5. `LICENSE` exists exactly when `LICENSE=MIT`.
6. The files the repository owns have their form:
   - `VERSION` is one line `X.Y.Z` or `X.Y.Z-dev`, without leading zeros;
   - each `VERSION_COPY` file holds its line for that version;
   - `.github/dependabot.yml` has a `github-actions` entry for `/`, with
     `open-pull-requests-limit: 0` in a repository that uses the standard (repo-standard's own
     entry has no limit);
   - each stage in `CI_STAGES` has its `ci/STAGE` or `ci/STAGE.ps1`;
   - `tests/helpers.bash`, where it exists, sources `.github/standard/tests/lib.bash`;
   - no tracked file holds gitleaks' inline allow marker.
7. On a pull request, a copy or block changes only on a `chore/standard-*` branch of this
   repository.
8. A path the standard removed does not come back: `.github/workflows/tests.yml`,
   `.github/workflows/validate.yml`, `.githooks/denylist.sh` and `.gitleaks.toml`.
9. In repo-standard only: the `MANIFEST` equals the one rebuilt from the tree, an error on the
   release pull request and a notice otherwise, and its own documents hold the blocks its config
   selects.
10. On a pull request, a change that loosens a check comes in a `chore/privacy-*` pull request of
    its own, from this repository, that changes only `.github/standard.conf`, `.gitleaksignore`
    and `CHANGELOG.md`. Loosening means:
    - a new `PRIVACY_ALLOW` or `PRIVACY_HISTORY_ACCEPT` line;
    - a dropped rule pack or CI stage;
    - a changed `ROLE`;
    - a raised `MAX_BLOB_KB`;
    - a switch to `RELEASE_KIND=legacy`;
    - any change to `.gitleaksignore`.

### The ci/ interface

`bin/run-stage STAGE` runs this repository's own `ci/STAGE` from the top of the repository, then
`ci/STAGE.ps1` under Windows PowerShell 5.1 (`-NoProfile -ExecutionPolicy Bypass`, with `TEMP`
and `TMP` set to `RUNNER_TEMP`). The stages are `lint`, `test` and `system`, as `CI_STAGES` lists
them. The stage's script must fail on a failed test and on a skipped one: CI runs every test.

### Workflows

`tests/workflows.bats` holds every workflow to these rules:
- each action is pinned to a commit SHA, with its version as a comment;
- only actions that `actions.allow` lists;
- every checkout sets `persist-credentials: false`;
- no workflow runs on `pull_request_target` or `workflow_run`;
- no `${{ }}` expression inside a `run:` script, so values reach scripts through `env:`;
- top-level `permissions` on every workflow, except one that only other workflows call;
- `timeout-minutes` on every job that runs steps;
- `GH_TOKEN` only in the env of a step that runs gh or a release tool;
- container and service images by digest.

It also checks the contracts of `ci.yml`, `secret-scan.yml` and `release.yml`. A legacy
`release.yml` or `image.yml` (`RELEASE_KIND=legacy`) meets the general rules except
`actions.allow`, and `tools/status` only warns about an extra allow-list entry while
`RELEASE_KIND=legacy`.

### The changelog and releases

`CHANGELOG.md` follows Keep a Changelog 1.1.0, and versions follow Semantic Versioning 2.0.0.
- A header names Keep a Changelog 1.1.0 and Semantic Versioning 2.0.0. Then come a
  `## [Unreleased]` section and one `## [X.Y.Z] - YYYY-MM-DD` section per release, newest first.
- The subsections come in this order: Migration, Added, Changed, Deprecated, Removed, Fixed,
  Security.
- A link block closes the file.

Every pull request adds a line under `## [Unreleased]`, with three exceptions:
- a release pull request (`chore/release-X.Y.Z`) moves those lines into its version;
- a draft fix (`fix/release-X.Y.Z-<slug>`) writes into that version's section;
- Dependabot adds none.

`bin/release-meta` tells whether a release is unreleased, pending or released, from `VERSION`,
`CHANGELOG.md` and the tags `vX.Y.Z`. `CONTRIBUTING.md`, Releasing, gives the steps.

### The secret scan

`secret-scan.yml` runs `bin/secret-scan`:
1. Positive controls first: gitleaks and every enabled rule pack must find a sample.
2. Then it scans the commits of the pull request or push, or the whole history on a schedule,
   a dispatch or a first push.
3. Gitleaks uses its default rules only, and `.gitleaks.toml` is refused.
4. An accepted finding names a commit that the base already holds.
