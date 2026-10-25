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

## Automated checks

- gitleaks scans every push and pull request (`.github/workflows/secret-scan.yml`).
- Dependabot keeps the pinned GitHub Actions, the Python dependencies and the Docker
  base image up to date.
