## What and why

## Phase

<!-- The phase this pull request holds, or "none". -->

## Release impact

<!-- patch, minor or major, and why (CONTRIBUTING.md, Releasing), or "none" when nothing that
ships changes. -->

## Checks on the target

<!-- What the tests cannot prove and a run on the target must (CONTRIBUTING.md, Checks), or
"none". -->

## Checklist

- [ ] One phase or one change in this pull request
- [ ] `CHANGELOG.md` has an entry under Unreleased, unless this is a release pull request or
      the fix of a draft release
- [ ] The checks in CONTRIBUTING.md, Checks, pass, and no test was skipped (no-skips reads bats
      output only, so check the log of any other test stage)
- [ ] No private data in the change, the title, this text or the branch name: no personal names,
      paths, host names, addresses, hardware or VM identifiers, real logs or secrets; examples
      use the synthetic values in CONTRIBUTING.md, Test data

CONTRIBUTING.md describes the workflow.
