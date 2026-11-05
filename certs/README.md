# Company root certificate (TLS inspection)

If your network inspects HTTPS traffic, the container cannot call GitHub or Azure until it trusts the company's root CA.

1. Get the company root CA from your IT or network team (a public certificate, not a secret). Do not use one from an untrusted source.
2. Save it here as `corporate-root-ca.crt` (PEM/Base64 text starting with `-----BEGIN CERTIFICATE-----`).
3. Rebuild: `docker compose build --no-cache` then `docker compose up -d`.

Any `*.crt` file in this folder is added to the container's trusted certificates. With none present, nothing changes.
Certificate files here are ignored by git.
