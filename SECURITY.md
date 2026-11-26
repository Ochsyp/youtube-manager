# Security policy

youtube-manager is a single-maintainer project.

<!-- standard:begin reporting -->
## Reporting a vulnerability

Report a vulnerability through GitHub's private vulnerability reporting: the **Security** tab,
then **Report a vulnerability**. Do not open a public issue.
<!-- standard:end reporting -->

## Secrets

- `JELLYFIN_TOKEN` and every other credential come from the environment; nothing
  secret is stored in this repository.
- The SQLite database in `data/` holds download history and subscription URLs. It is
  runtime state, and `.gitignore` and `.dockerignore` keep it out of git and images.
- Never paste tokens, keys or logs that contain them into issues, pull requests or commits.
- If a secret is ever committed, rotate it first, then remove it from history:
  deleting the file leaves it in earlier commits.

## Known exposure

The web UI has no authentication. Run it only on a trusted network or behind an
authenticating reverse proxy.

It does refuse requests that other web pages make through a visitor's browser:

- Host pinning: any `Host` other than `youtube-manager`, `localhost`, `127.0.0.1` or an
  entry of the comma-separated `YTM_ALLOWED_HOSTS` (the name the reverse proxy serves the
  UI under) gets 400, which blocks DNS rebinding. The port is not compared.
- A cross-origin guard on every request other than GET, HEAD and OPTIONS. It accepts a
  `Sec-Fetch-Site` of `same-origin` or `none`, or, when the browser sends no
  `Sec-Fetch-Site`, an `Origin` whose host and port equal `Host`. Anything else,
  including a missing or `null` Origin, gets 403 and one WARN line in the app log.
  Manual curl POSTs need `-H 'Origin: http://<ui-name>'`.
- A Content-Security-Policy, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`
  and same-origin Referrer and Cross-Origin-Resource policies on every response.

None of this stops a client on the network that sends the right headers itself. Keep
the UI on a trusted network: publish the container's port only to the reverse proxy, and
let the proxy admit only the clients and probes that should reach it. A client that
reaches the port directly needs the address it uses in `YTM_ALLOWED_HOSTS`, or it gets
400.

<!-- standard:begin sealed-releases -->
## Sealed releases

Hosts install a release of this repository only when the maintainer has sealed it. A seal is an
annotated tag `seal/vX.Y.Z` with the message `<repository> vX.Y.Z`, signed with the
maintainer's offline SSH key, on the commit of the published release `vX.Y.Z`. The maintainer
seals after the publish, with a seal tool that first checks the release: published, immutable,
neither a draft nor a pre-release, drafted by GitHub Actions, its commit on `main`, its `verify`
run green, and `SEAL=yes` set once in `.github/standard.conf` at that commit. The tool refuses a
release made before the repository set `SEAL=yes`.

A host checks the seal with `git verify-tag` against its own root-owned list of allowed signers.
It also checks the seal's message, that the seal and `vX.Y.Z` name the same commit, that this
commit is on `main`, that no tag it has seen has moved, and that the version is newer. It needs
no GitHub token for this and calls no API. A stolen GitHub token or session can publish a
release but cannot seal it, so no host installs that release. A seal never moves and is never
deleted. A release that must not be installed stays unsealed, or is held on the hosts as the
Release contract in `CONTRIBUTING.md` says.
<!-- standard:end sealed-releases -->

## Image and release checks

- CI builds the image for amd64 and arm64 on every pull request and on main, and smoke-tests each
  build with `ci/image-smoke`.
- A merged release pull request builds the release image without the layer cache, so it has the
  Debian updates of that day. It is pushed by digest only, after both builds pass the smoke test,
  with provenance and an SBOM, and attested by digest. The draft release names that digest.
- GitHub creates the tag `vX.Y.Z` when the maintainer publishes the release, and only then do
  the tags `X.Y.Z` and `latest` point at the digest. The maintainer's signature is the seal
  `seal/vX.Y.Z`, not the release tag.
- Dependabot checks the exact pins in `requirements.txt` daily. yt-dlp, its solver and its
  PO-token plugin skip the seven-day wait the other packages get, because their releases follow
  YouTube's changes. It also keeps the base image digest and deno current.

<!-- standard:begin automated-checks -->
## Automated checks

- **Secret scan.** The `secret-scan` workflow runs gitleaks (MIT, pinned by version and SHA-256,
  always with its default rules) and this repository's rule packs. It scans the commits of each
  pull request and each push to `main`, and the whole history every Monday and when run by hand.
  Positive controls run first, so a scanner that finds nothing fails. Findings are redacted.
  GitHub's secret scanning and push protection are on as well. GitHub turns the Monday run off
  after 60 days without activity in this repository.
- **Hooks.** The hooks in `.githooks/` check every commit and push on the maintainer's machine:
  content, path names, messages, ref names and identities, against the rule packs and a denylist
  that never leaves that machine. The maintainer also checks every day what reached GitHub, pull
  request texts and release notes included.
- **CI and releases.** The `ci` workflow checks the standard's copies, the privacy rules over the
  tree, the version, the changelog and the pull request, and runs the tests. The `release`
  workflow builds, attests and drafts each release, and verifies it once it is published.
- **Dependencies.** Dependabot watches the ecosystems in `.github/dependabot.yml`, with security
  updates on.

If a secret is ever committed, rotate it first, then remove it from the history.
<!-- standard:end automated-checks -->
