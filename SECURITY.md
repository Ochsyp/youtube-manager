# Security policy

youtube-manager is a single-maintainer project.

## Reporting a problem

Report a vulnerability privately through GitHub's
[private vulnerability reporting](https://github.com/Ochsyp/youtube-manager/security/advisories/new),
not in a public issue. Never paste tokens, keys or logs that contain them into issues,
pull requests or commits.

## Secrets

- `JELLYFIN_TOKEN` and every other credential come from the environment; nothing
  secret is stored in this repository.
- The SQLite database in `data/` holds download history and subscription URLs. It is
  runtime state, and `.gitignore` and `.dockerignore` keep it out of git and images.
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

## Automated checks

- gitleaks scans every push and pull request (`.github/workflows/secret-scan.yml`).
- CI builds the image for amd64 and arm64 on every pull request and on main, and
  smoke-tests each build (`.github/workflows/image.yml`). A release builds the image
  without the layer cache, so it has the Debian updates of that day, and pushes it only
  after both of its builds pass the same smoke test.
- Dependabot proposes updates to the exact pins in `requirements.txt`, checked daily, with
  yt-dlp, its solver and its PO-token plugin exempt from the seven-day wait the other
  packages get. Its security updates are on as well and skip that wait for every package.
  It also keeps the base image digest, deno and the pinned GitHub Actions current.
- Release images carry provenance and SBOM attestations.
