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
request text. That pull request changes nothing else.

## Test

```sh
.github/standard/bin/install-tools bats
bats .github/standard/tests/
```

`install-tools` installs the pinned bats and prints its folder. In Git Bash on Windows, run the
suites one file at a time, and leave `lint.bats` to Linux. CI runs them in the job `standard`.
