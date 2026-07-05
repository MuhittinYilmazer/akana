# Remote access and Telegram

Akana is a personal server, not a cloud service. This page covers reaching it from your phone over a private tailnet plus the optional Telegram bridge. For short overviews, see [Remote access](../README.md#remote-access--telegram) in the README.

## Remote access over Tailscale

The supported way to reach Akana from your phone or tablet is [Tailscale](https://tailscale.com/) Serve on top of a private tailnet.

### One-time setup

1. In `.env`, set `AKANA_TOKEN` to a random secret (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`). Leave `AKANA_HOST=127.0.0.1`.
2. Install Tailscale on the PC running Akana and on your phone, then sign into the same tailnet.
3. Start Akana, then from **Settings → Connection** expose the loopback port over Tailscale Serve (or run `tailscale serve --bg http://127.0.0.1:8766` directly). You get an HTTPS URL like `https://your-host.tailnet.ts.net`.

### Pair the phone

Open the desktop cockpit, click **Connect phone**, then scan the QR with the phone camera. The QR carries the bearer token in a URL fragment (`/#token=…`); the phone-side UI writes it to local storage and immediately strips it from the URL. No typing. Once paired, use "Add to Home Screen": the manifest declares Akana as a standalone PWA with an offline fallback shell.

### Safety rails baked in

- Binding to a non-loopback address (`0.0.0.0`, a LAN IP) with an empty `AKANA_TOKEN` makes the process refuse to start (override only via the explicit `AKANA_ALLOW_UNAUTHENTICATED=1` opt-in).
- Any request that reached the server through a reverse proxy (Tailscale Serve, nginx, etc.) must present the bearer, even if the backend socket is loopback.
- Tailscale **Funnel** (public-internet exposure) is refused with `FUNNEL_REQUIRES_TOKEN` unless `AKANA_TOKEN` is set, so an unauthenticated instance is never published to the public internet.
- The `/api/v1/system/pair` endpoint that serves the QR payload is loopback-only; the raw token cannot be exfiltrated over the tunnel.

### Why HTTPS matters for voice

Browsers require a secure context for microphone access and for Service Worker / PWA install. Tailscale Serve terminates TLS with an automatic tailnet certificate, so voice mode and "Add to Home Screen" work over the tunnel. A plain-http tailnet IP is fine for text chat, but the mic and PWA install will be disabled.

### Auth headers

REST calls use `Authorization: Bearer <AKANA_TOKEN>`. WebSocket endpoints (`/ws/events`, `/ws/voice/live`, `/ws/voice/realtime`) accept the same token via `?token=…` query parameter, because browsers cannot attach Authorization headers to WebSocket handshakes. No other query-string auth exists.

## Telegram

Akana includes an optional Telegram bridge, so your local assistant can be reached from your phone the same way you would chat with any other bot.

- **Set it up.** Create a bot with Telegram's [@BotFather](https://t.me/BotFather) and copy the token it gives you. Paste it into the settings panel (encrypted at rest) or set `AKANA_TELEGRAM_BOT_TOKEN` in `.env`, then flip `AKANA_TELEGRAM_ENABLED=1`.
- **Lock it down.** Add your own Telegram chat_id to `AKANA_TELEGRAM_ALLOWED_CHAT_IDS` (comma-separated). This allowlist is **mandatory**: with no ids set, the poll loop refuses to start and no one can talk to your bot. Messages from any chat_id not on the list are silently ignored, so the bot never confirms its existence to strangers. Denied chats are audit-logged as `connector_chat_denied`.
- **How chatting works.** Each allowed Telegram chat is bound to its own persistent Akana conversation, visible in the web UI's conversation list as `Telegram: <your name>`. History follows across messages (last N turns, trimmed to `AKANA_CONNECTOR_HISTORY_BUDGET` characters, default 12000), so the bot remembers what you were talking about. Slash commands: `/yeni` starts a fresh conversation, `/durum` reports the current provider and active channels, `/baglan` hooks this Telegram chat into whatever conversation you last had open in the web UI (handy for continuing a desktop chat from your phone).
- **Per-channel persona.** You can give the Telegram side its own voice: either bind a persona through the API, or set `AKANA_PERSONA_TELEGRAM=<persona_id>` in `.env`. Priority order is skill persona > conversation override > channel binding > the default `akana` persona.
- **Limits to know.** Text only: photos, voice notes and other attachments are ignored. Replies aren't streamed; you get one message per turn (Akana auto-splits anything over Telegram's 4096-char cap). Transport is long-polling (`getUpdates`), so there is no public URL to expose. Every outbound message passes through the egress filter (secret/PII redaction), so reminders and proactive pushes are protected too.

> **Note:** Telegram is the only `restart_required` setting; it lives on a hidden Channels tab. Every other runtime setting applies live.
