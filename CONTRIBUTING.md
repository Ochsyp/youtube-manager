# Contributing

youtube-manager downloads YouTube videos into a media library. One maintainer runs it.

<!-- standard:begin workflow -->
## Workflow

Every change reaches `main` through a pull request that the maintainer reviews and merges. This
holds for the maintainer too.

1. Branch from an up-to-date `main`: `phase/<n>-<slug>` for a phase of the plan, or `fix/`,
   `feat/`, `docs/` or `chore/` plus a short slug.
2. Write commit messages and the pull request title as Conventional Commits (`feat:`, `fix:`,
   `docs:`, `chore:`, `ci:`, `test:`, `refactor:`).
3. Add a line under `## [Unreleased]` in `CHANGELOG.md`. Three kinds of pull request add none
   there: a release pull request (`chore/release-X.Y.Z`), which moves the lines into the new
   version; the fix of a draft release (`fix/release-X.Y.Z-<slug>`), whose line goes into that
   version's section; and a Dependabot pull request, whose updates the next release pull request
   sums up.
4. Run the checks in Checks below, and fix what they find.
5. Push the branch and open a pull request against `main`. Before every `gh pr create` and
   `gh pr edit`, run `.github/standard/bin/pr-text` on the title and the body: it refuses
   denylist terms and rule pack hits, and never prints them.
6. Start the next phase only after the pull request is merged.

One pull request holds one phase or one change.

Repository work happens only on a machine with the maintainer's git configuration, never in a
hosted environment such as a codespace or a cloud workspace. That configuration turns the hooks
on and loads the denylists for every clone of this repository's GitHub URL. A clone made from a
local path gets no hooks, so run `git config core.hooksPath .githooks` in it right after cloning.

An outside contributor's pull request is reviewed in the browser and through CI. Hooks run from
the checked-out tree with the maintainer's credentials, even during a fetch, so:
- never check out a fork's pull request branch in a clone with the hooks on;
- clone a fork only with `git clone <fork URL>` or `gh repo clone <fork> <folder> --no-upstream`,
  into a scratch folder, and never add a remote for this repository there.
<!-- standard:end workflow -->

<!-- standard:begin guard-rails -->
## Guard rails

GitHub's ruleset on `main` blocks direct pushes, force pushes and its deletion, and the merge
of a pull request whose `all-passed` or `scan` check failed.

The hooks in `.githooks/` guard every commit and push:
- `pre-commit` refuses a commit while `main` or `master` is checked out. It also refuses a changed
  copy of the standard, a staged file above `MAX_BLOB_KB`, and staged content, path names, author
  or committer that hold a denylist term or a rule pack hit. `pre-merge-commit` does the same for
  a merge.
- `commit-msg` refuses such a term or hit in the message.
- `pre-push` refuses a push to `main` or `master`, and a branch or tag name with a term or hit. It
  refuses every tag push except a `v*` tag under `RELEASE_KIND=legacy`; otherwise release tags
  come from publishing a release. It also refuses a term or hit in what the pushed commits bring
  in, which covers rebase, cherry-pick and `am` too.

The denylists hold names that must never be committed and never leave the maintainer's machine.
The rule packs in `.githooks/rules/` hold the patterns: private addresses, local host names, MAC
addresses, home paths and e-mail addresses, and disk and VM identifiers where the config enables
them. A refusal names the file, the line and the kind of finding, never the value.

The maintainer's git configuration turns the hooks on for every clone of this repository's GitHub
URL. Anywhere else, such as a clone made from a local path, turn them on once after cloning:
`git config core.hooksPath .githooks`. Never use `--no-verify`.
<!-- standard:end guard-rails -->

<!-- standard:begin standard -->
## The repository standard

Files listed in `.github/standard/MANIFEST` are copies of the repository standard. Never edit
them here: change repo-standard, release it, and sync with a `chore/standard-X.Y.Z` pull request.
CI fails when a copy differs or changes on another branch. An exemption, an accepted finding or a
dropped check comes in a `chore/privacy-*` pull request of its own.

When a private repository goes public, it does so in this order. A pull request sets
`VISIBILITY=public`, and `tools/sync` writes the public block texts into it. The maintainer
switches the repository to public on GitHub and turns on the security settings and the ruleset
those texts promise. A new commit on that branch reruns its checks; a rerun from the Actions page
keeps the old private flag. Then that pull request merges before anything else: until it does,
`main` says private while GitHub says public, and CI on `main` and `tools/status` fail.
<!-- standard:end standard -->

## Checks

Run the test suite from the repository root, as the README's Running tests section describes:

```text
uv run --no-project --python 3.13 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest -q
```

Tests marked `posix` need `os.killpg` and `SIGKILL`, so they skip on Windows. CI runs `ci/test`
on Linux, where a skipped test fails the run. CI's `image` job also builds the image for amd64 and
arm64 and smoke-tests each build with `scripts/smoke-test-image.sh`; it pushes nothing. On Linux
with Docker, `bash scripts/smoke-test-image.sh youtube-manager` runs the same smoke test locally.

## Test data

Examples and tests use made-up values only: reserved example host names, video ids such as
`dQw4w9WgXcQ` and `smoketest01`, and the stand-in for yt-dlp in `tests/fakes/yt_dlp_fake.py`. The
privacy rules are the `base` pack.

## Schema changes

The database's schema version is its `PRAGMA user_version`, and `SCHEMA_VERSION` in
`youtube_manager/database.py` is the version a release creates and migrates to. A schema change
adds a step to `MIGRATIONS` there, keyed by the version it brings the database to, and raises
`SCHEMA_VERSION` to that key. At start the web UI copies the database into `data/backups`, then
runs each pending step in one transaction with the version it reaches.

- A step the previous release keeps working through takes the next version in its hundred, such
  as 2 after 1, and the release can be a minor. Such a step may add a nullable column, a
  non-unique index or a table, and fill in values the previous release reads the same way. It
  may also add a column with a DDL default (`server_default`). An ORM `default=` alone never
  reaches the DDL, so a file created fresh would refuse the previous release's inserts.
- Any other step takes the next hundred, such as 101 after 1: a dropped, renamed or retyped
  column, a NOT NULL column without a DDL default, a UNIQUE or CHECK constraint. Releases that
  know schema versions refuse the migrated file and older ones would misread it, so the release
  is a major, with the `### Migration (cannot be rolled back)` subsection described below.
- A step runs against a live file and against today's ORM. It must therefore be a no-op on a
  database it finds already done, including columns, tables and indexes that an earlier step
  already created from the ORM.
- Freeze the new version's schema as `tests/schemas/v<version>.sql`, from a file the release
  creates fresh. The tests compare the newest frozen schema with a new file down to column
  types, defaults and index uniqueness, and migrate a file at every frozen version to the ORM's
  tables, columns and indexes, so a schema change without its step and its frozen file fails
  them. They also upgrade a file as a 1.0.x release creates it, version 1's schema with no
  `user_version`, and compare the result with the newest frozen schema, NOT NULL included.

<!-- standard:begin releasing -->
## Releasing

GitHub Actions builds each release, and the maintainer publishes it in the browser, then seals
it: the maintainer's own hosts install a sealed release only. Anyone else downloads and installs
a release without a seal.

1. `.github/standard/bin/release-prepare` writes the release pull request `chore/release-X.Y.Z`:
   `VERSION`, the `VERSION_COPY` files, and the `CHANGELOG.md` section with its links. Its body
   names the dependency updates since the last release. CI checks the version, the date and the
   links. For a zip, tar or stack release, the `release` workflow builds it twice and compares
   the builds.
2. The maintainer reviews and squash-merges it. The push to `main` that changes `VERSION` runs
   the `release` workflow: the checks of `ci` on that commit, the build, an attestation in the
   public repositories, and a draft release `vX.Y.Z` of that commit.
3. The maintainer checks the draft (notes, assets, target commit, image digest) and clicks
   **Publish release**. GitHub creates the tag and the immutable release. The `verify` job then
   checks the published release, and the maintainer looks for it to pass.
4. Then the maintainer seals the release. Double-click this repository's seal shortcut and check
   what the dialog shows: the version, its commit, the release pull request, the commits since
   the last seal and each check the tool ran. Click **Seal** and type the passphrase of the
   offline signing key. The tool pushes the annotated, SSH-signed tag `seal/vX.Y.Z` on the
   release's commit, from a mirror of its own; `pre-push` refuses `refs/tags/seal/*` from every
   clone.
5. To fix a draft, merge a pull request from `fix/release-X.Y.Z-<slug>`; its `CHANGELOG.md` line
   goes into `## [X.Y.Z]`. Then run the `release` workflow on `main` (Actions, release, Run
   workflow). Merge nothing else until the draft is published: CI says so on every pull request
   while a release waits.
6. Never delete a published release, and never move or delete a seal: a host stops on a seal
   that moved. A tag ruleset enforces it: target tags, `refs/tags/seal/*`, the rules deletion
   and update, no bypass. It leaves creating a seal open. The maintainer sets it once, when the
   repository starts to seal. To stop a release before its seal, leave it unsealed and tick
   **Set as a pre-release**, so repo-standard's `tools/status` stops asking for its seal; then
   ship X.Y.Z+1. To stop a sealed release, use the hold that the Release contract names, and ship
   X.Y.Z+1. For a container image, the pre-release tick also moves `latest` back.

Only the maintainer publishes a release. No one creates a release tag or a seal by hand, and no
script or automation publishes a release, or edits or deletes a published one.
<!-- standard:end releasing -->

## Release contract

- **What a release is.** One container image for linux/amd64 and linux/arm64, at
  `ghcr.io/ochsyp/youtube-manager`. The "Container image" section of the release notes names it
  by version and digest, `ghcr.io/ochsyp/youtube-manager:X.Y.Z@sha256:...`, and a host pins that
  whole reference. The image is attested by that digest, and the release's assets `image.json`
  and `SHA256SUMS` name it.
- **The host.** The maintainer's host deployment runs youtube-manager. Once it switches to this
  image, it runs the image pinned by version and digest in its own Compose file and never pulls
  `latest`; until then, it builds youtube-manager from a clone. The host does not check this
  repository's seal itself: it installs only sealed releases of its own deployment, and those
  carry the pin. Before a reviewed change moves the pin, whoever writes that change verifies
  `seal/vX.Y.Z` against the release key, and checks that the digest it pins is the one this
  release names. This is how the host meets the seal check in `SECURITY.md`, Sealed releases:
  the host's own sealed release carries the pin, and that pin moves only after this
  repository's seal has been verified.
- **The hold.** A release that is published but not yet sealed, or whose seal does not verify, is
  held: the pin stays on the last sealed release, and the host keeps running that image
  unchanged. Such a release reaches the host only after it is sealed and a new sealed release of
  the host deployment carries its pin. A release that must not reach it is never sealed, and a
  later sealed release replaces it.

## Versioning

Releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version covers the
image and its interface: the environment variables, the volumes, the `web` and `worker` commands
and the database.

- **Major**: anything a host has to change by hand, or a database migration the previous release
  cannot run on. The CHANGELOG section of a release with such a migration has a
  `### Migration (cannot be rolled back)` subsection.
- **Minor**: new features and settings, and additive schema changes the previous minor release
  still runs on.
- **Patch**: fixes and dependency updates, yt-dlp included. Dependabot's yt-dlp pull requests
  become patch releases. When one raises the bgutil plugin, raise the provider image in
  `compose.example.yaml` to the same release in that pull request; the tests fail until they
  match. A host's provider of the same major release keeps working, with a warning in the log.

`release-prepare` also sets the README's install command to `YTM_VERSION=X.Y.Z` (`VERSION_COPY`
in `.github/standard.conf`), and `standard-check` fails until it matches.
