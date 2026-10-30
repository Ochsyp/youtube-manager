# Security policy

This is a private, single-maintainer repository.

## Reporting a problem

Contact [@Ochsyp](https://github.com/Ochsyp) directly. Never paste tokens, keys or logs
that contain them into issues, pull requests or commits.

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
  entry of the comma-separated `YTM_ALLOWED_HOSTS` (the UI's nginx name) gets 400, which
  blocks DNS rebinding. The port is not compared.
- A cross-origin guard on every request other than GET, HEAD and OPTIONS. It accepts a
  `Sec-Fetch-Site` of `same-origin` or `none`, or, when the browser sends no
  `Sec-Fetch-Site`, an `Origin` whose host and port equal `Host`. Anything else,
  including a missing or `null` Origin, gets 403 and one WARN line in the app log.
  Manual curl POSTs need `-H 'Origin: http://<ui-name>'`.
- A Content-Security-Policy, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`
  and same-origin Referrer and Cross-Origin-Resource policies on every response.

None of this stops a client on the network that sends the right headers itself. Keep
the UI LAN-only: in jelly-stack, publish no host port for it (close `8085`) and let
the nginx server for the UI's name allow only the LAN and the stack's Docker subnets.
Until port 8085 is closed, `http://<pi-ip>:8085` answers 400 unless
`YTM_ALLOWED_HOSTS` lists the Pi's address.

## Automated checks

- gitleaks scans every push and pull request (`.github/workflows/secret-scan.yml`).
- Dependabot keeps the pinned GitHub Actions, the Python dependencies and the Docker
  base image up to date.
