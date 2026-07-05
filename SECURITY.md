# Security Policy

Akana is a **self-hosted** personal assistant: the server, the web UI, and your data run
on your own machine. Your conversations and memory stay local; only the model provider you
choose ever receives the messages you send it. Even so, Akana handles sensitive material —
provider API keys and OAuth tokens in an encrypted vault, and an optional bearer token for
remote access — so we take security reports seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's **[Security Advisories](https://github.com/MuhittinYilmazer/akana/security/advisories/new)**
(the repository's **Security** tab → **Report a vulnerability**). Please include:

- what the issue is and its impact,
- steps to reproduce (a minimal proof-of-concept if you have one),
- the affected version / commit, your OS, and the provider in use.

We aim to acknowledge a report within a few days and to agree on a disclosure timeline with
you. Please give us a reasonable window to ship a fix before any public disclosure.

## In scope

Issues that let an attacker:

- read or exfiltrate the encrypted vault, its master key, or `secrets.json`;
- bypass the `AKANA_TOKEN` bearer gate on `/api/v1/*` (for example through a reverse proxy);
- read or write arbitrary files, or execute code, via the server, an MCP child, or a pack;
- leak one provider's credentials to another provider or to a spawned tool.

## Out of scope

- Attacks that require an already-compromised host, or a malicious pack the operator
  installed on purpose.
- The content a **cloud** model provider receives — that is inherent to using a cloud model.
  Choose a local provider (e.g. Ollama) to keep everything offline.
- Missing hardening on a deployment the operator deliberately exposed without a token. Akana
  already refuses a non-loopback bind with an empty token and rejects proxied requests
  without one (see the `AKANA_TOKEN` notes in [`.env.example`](.env.example)).

## Good practices for operators

- Keep `AKANA_TOKEN` set for any non-local (phone / proxied / remote) access.
- Keep the vault master key (`~/.config/akana/vault.key`, mode `0600`) off shared storage.
- Keep your Python and Node toolchains — and Akana's dependencies — up to date. Dependabot
  is enabled for this repository.
