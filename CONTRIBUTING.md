# Contributing

Every change reaches `main` through a pull request, so that it is reviewed before it is merged.
This holds for the maintainer too.

## Workflow

1. Start from an up-to-date `main` and create a branch for the change:
   - `phase/<n>-<slug>` for a phase of a planned redesign, for example
     `phase/6-stable-episode-numbering`;
   - `fix/<slug>`, `feat/<slug>`, `docs/<slug>` or `chore/<slug>` for anything else.
2. Commit on that branch. Commit messages follow
   [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), for example
   `fix: keep filed episode numbers stable`.
3. Add an entry for the change under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md), on the
   same branch.
4. Run the tests as the README's Running tests section describes.
5. Push the branch and open a pull request against `main`. CI and the secret scan run on it.
   Besides the tests, CI's image job builds the image for amd64 and arm64 and smoke-tests each
   build; it pushes nothing.
6. Start the next phase only after the pull request is reviewed and merged. Its branch starts from
   the updated `main`.

One pull request holds one phase or one change.

## Guard rails

`main` is never committed to or pushed to directly. A GitHub ruleset blocks force pushes to `main`
and its deletion, but not a direct push, so two git hooks in `.githooks` enforce it on each clone:

- `pre-commit` refuses a commit while `main` or `master` is checked out;
- `pre-push` refuses a push to `main` or `master`.

Turn them on once after cloning:

```text
git config core.hooksPath .githooks
```

Do not bypass them with `--no-verify`. After a pull request is merged, bring the local `main` up
to date with `git switch main` and `git pull --ff-only`.

## Schema changes

The database's schema version is its `PRAGMA user_version`, and `SCHEMA_VERSION` in
`youtube_manager/database.py` is the version a release creates and migrates to. A schema change
adds a step to `MIGRATIONS` there, keyed by the version it brings the database to, and raises
`SCHEMA_VERSION` to that key. At start the web UI copies the database into `data/backups`, then
runs each pending step in one transaction with the version it reaches.

- A step the previous release keeps working through takes the next version in its hundred, such
  as 2 after 1, and the release can be a minor. Such a step may add a nullable column, a column
  with a DDL default (`server_default`; an ORM `default=` alone never reaches the DDL, so a file
  created fresh would refuse the previous release's inserts), a non-unique index or a table, and
  fill in values the previous release reads the same way.
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
   `https://github.com/Ochsyp/youtube-manager/releases/tag/v1.0.0`.
2. After the merge, sign an annotated tag on the commit the merge created on `main`, which the
   `gh pr view` line below prints, so that nothing merged after it ships under this version. The
   tag message is `youtube-manager vX.Y.Z`. Tags are signed with the release signing key: one
   ed25519 SSH key, added to GitHub as a signing key titled `release signing` so that the tag shows
   as verified, and listed as `maintainer namespaces="git" <public key>` in the allowed signers
   file `git tag -v` reads (`gpg.ssh.allowedSignersFile`). Git signs with it once `gpg.format` is
   `ssh` and `user.signingkey` names the key. Then check the signature and push the tag:

   ```text
   git switch main
   git pull --ff-only
   gh pr view <number> --json mergeCommit --jq .mergeCommit.oid
   git tag -s vX.Y.Z -m "youtube-manager vX.Y.Z" <merge commit>
   git tag -v vX.Y.Z
   git push origin vX.Y.Z
   ```

3. Check the tag before publishing, because publishing is final. Releases in this repository
   are immutable: a published release locks its tag, and deleting the release does not free the
   tag name. If the release workflow fails after that, the fix ships as the next patch release.

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

4. Publishing starts the release workflow (`.github/workflows/release.yml`). It checks that the tag
   is vX.Y.Z on a commit of `main` and that CHANGELOG.md has its dated heading, builds and
   smoke-tests the image for amd64 and arm64, pushes `ghcr.io/ochsyp/youtube-manager:X.Y.Z` and
   `latest` with provenance and SBOM attestations, and appends the image's digest to the release
   notes. Keep that `## Container image` section in every release's notes: deployments that
   follow releases read the image digest from it.

A release marked as a prerelease is pushed without `latest`, and promoting it to a full release
later does not run the workflow again.

First release only: check the package's visibility on its GitHub page. If it is private, make it
public under Package settings, Change visibility. This cannot be undone.
