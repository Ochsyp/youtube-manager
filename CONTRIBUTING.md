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
2. After the merge, sign a tag on the commit the merge created on `main`, which the `gh pr view`
   line below prints, so that nothing merged after it ships under this version. `git tag -s`
   needs a signing key set up in Git (`gpg.format` and `user.signingkey`), and the key added to
   GitHub as a signing key, so that the tag shows as verified. Then push the tag:

   ```text
   git switch main
   git pull --ff-only
   gh pr view <number> --json mergeCommit --jq .mergeCommit.oid
   git tag -s vX.Y.Z -m vX.Y.Z <merge commit>
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
   notes.

A release marked as a prerelease is pushed without `latest`, and promoting it to a full release
later does not run the workflow again.

First release only: check the package's visibility on its GitHub page. If it is private, make it
public under Package settings, Change visibility. This cannot be undone.
