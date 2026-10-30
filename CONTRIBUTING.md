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
6. Start the next phase only after the pull request is reviewed and merged. Its branch starts from
   the updated `main`.

One pull request holds one phase or one change.

## Guard rails

`main` is never committed to or pushed to directly. GitHub cannot enforce this for a private
repository on the Free plan, so two git hooks in `.githooks` enforce it on each clone:

- `pre-commit` refuses a commit while `main` or `master` is checked out;
- `pre-push` refuses a push to `main` or `master`.

Turn them on once after cloning:

```text
git config core.hooksPath .githooks
```

Do not bypass them with `--no-verify`. After a pull request is merged, bring the local `main` up
to date with `git switch main` and `git pull --ff-only`.
