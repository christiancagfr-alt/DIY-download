# Security Policy

## Supported version

Security fixes are maintained on the latest `main` branch and the latest GitHub Release.

## Reporting a vulnerability

Do not post credentials, tokens, private Drive links, private files, or exploit details in a public issue.

Preferred reporting path:

1. Use GitHub private vulnerability reporting / Security Advisories for this repository when available.
2. If that is unavailable, contact the repository owner privately before disclosing exploit details publicly.

## Local secrets

The application intentionally keeps OAuth credentials and authorization tokens out of the repository.

Never commit:

- Google OAuth client secret JSON
- Google service-account JSON
- `token.json`
- Apple signing certificates or provisioning material
- API tokens / access keys / webhooks
- `.env` files containing values

If a credential has ever been committed or otherwise exposed, deleting it from the current source is not sufficient. Revoke/rotate it at the provider.

## Release security

Official releases are expected to be produced only by `.github/workflows/release.yml`.

The release pipeline:

- requires hash-locked Python dependencies
- runs dependency and static security gates
- builds Windows, macOS arm64, and macOS Intel packages
- requires Developer ID signing and Apple notarization for macOS
- generates SHA-256 checksums
- generates GitHub build provenance attestations
- pins third-party GitHub Actions to full commit SHAs

The application updater accepts only HTTPS GitHub Release assets from the configured personal repository and requires the GitHub-provided SHA-256 asset digest before treating an update as valid.

## Remaining design-sensitive areas

Some security changes can alter product behavior and therefore require explicit developer approval:

- blocking downloads from private/LAN IP addresses in the generic public URL downloader
- replacing full Google Drive OAuth scope with narrower scopes if future functionality permits it
- replacing the currently unmaintained `google-auth-httplib2` transport with a different Google API transport

These items are tracked in `SECURITY_AUDIT_REPORT.md`.
