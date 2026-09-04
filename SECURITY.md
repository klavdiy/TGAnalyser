# Security Policy

## Reporting

If you find a vulnerability, use GitHub **Report a vulnerability** on this repository. Do not open a public issue.

## Credentials

Never commit:

- `.env`
- Telegram session files (`*.session`)
- cloud passwords
- API id / API hash values

Copy `.env.example` to `.env` and keep `.env` only on your machine or in encrypted GitHub Actions secrets.

## Supported versions

The `main` branch is the only supported line.
