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
cloud session. That configuration turns the hooks on and loads the denylists for every clone of
this repository's GitHub URL. A clone made from a local path gets no hooks, so run
`git config core.hooksPath .githooks` in it right after cloning.

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
CI fails when a copy differs or changes on another branch. Sync summaries and pull request texts
here name no other repository. An exemption, an accepted finding or a dropped check comes in a
`chore/privacy-*` pull request of its own.

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

## Releasing

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

To release X.Y.Z:

1. Open a release pull request from `chore/release-X.Y.Z`. It moves the entries under
   `## [Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section, the heading the release
   workflow looks for, and updates the compare links at the end of CHANGELOG.md:

   ```text
   [Unreleased]: https://github.com/Ochsyp/youtube-manager/compare/vX.Y.Z...HEAD
   [X.Y.Z]: https://github.com/Ochsyp/youtube-manager/compare/vP.Q.R...vX.Y.Z
   ```

   vP.Q.R is the previous release. The first release links to its tag instead, as in
   `https://github.com/Ochsyp/youtube-manager/releases/tag/v1.0.0`. The same pull request sets
   `VERSION` to `X.Y.Z` and the README's install command to `YTM_VERSION=X.Y.Z`; a test fails until
   the README matches. Its title is `chore(release): X.Y.Z`.
2. After the merge, sign an annotated tag on the merge commit on `main`, which the `gh pr view`
   line below prints. Then nothing merged after it ships under this version. The tag message is
   `youtube-manager vX.Y.Z`.
   - The release signing key is one ed25519 SSH key. It is added to GitHub as a signing key
     titled `release signing`, so the tag shows as verified.
   - It is listed as `maintainer namespaces="git" <public key>` in the allowed signers file that
     `git tag -v` reads (`gpg.ssh.allowedSignersFile`).
   - Git signs with it once `gpg.format` is `ssh` and `user.signingkey` names the key.

   Then check the signature and push the tag:

   ```text
   git switch main
   git pull --ff-only
   gh pr view <number> --json mergeCommit --jq .mergeCommit.oid
   git tag -s vX.Y.Z -m "youtube-manager vX.Y.Z" <merge commit>
   git tag -v vX.Y.Z
   git push origin vX.Y.Z
   ```

3. Check the tag before publishing, because publishing is final. Releases here are immutable: a
   published release locks its tag, and deleting the release does not free the tag name. If the
   release workflow fails after that, the fix ships as the next patch release.

   ```text
   uv run --no-project --python 3.13 python scripts/check_release.py vX.Y.Z
   git branch -r --contains vX.Y.Z
   gh run list --commit <merge commit>
   ```

   The first prints X.Y.Z, the second lists `origin/main`, and the third shows CI green on the
   merge commit, its image job included. Then publish the release. Its notes come from a file
   holding that version's CHANGELOG section:

   ```text
   gh release create vX.Y.Z --verify-tag --title vX.Y.Z --notes-file <notes file>
   ```

4. Publishing starts the release workflow (`.github/workflows/release.yml`):
   - it checks that the tag is vX.Y.Z on a commit of `main` and that CHANGELOG.md has its dated
     heading;
   - it builds and smoke-tests the image for amd64 and arm64;
   - it pushes `ghcr.io/ochsyp/youtube-manager:X.Y.Z` and `latest` with provenance and SBOM
     attestations;
   - it appends the image's digest to the release notes.

   Keep that `## Container image` section in every release's notes: deployments that follow
   releases read the image digest from it.

A release marked as a prerelease is pushed without `latest`, and promoting it to a full release
later does not run the workflow again.

First release only: check the package's visibility on its GitHub page. If it is private, make it
public under Package settings, Change visibility. This cannot be undone.
