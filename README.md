# Sewa Setu — Old Age Pension Portal

Handover copy. Vendor maintenance contract ended 31/01/2026.

## Running

```
cp .env.example .env
# Replace both placeholder values in .env before continuing.
docker compose up --build
```

`EXPOSE_SMS_GATEWAY=true` is for local testing so you can read OTPs at
`/__gateway/`. Set it to `false` before any real public deployment; a real SMS
provider must never expose citizens' messages through a public web page.

- Portal: http://localhost:8000
- SMS gateway console (simulated telecom): http://localhost:8000/__gateway/ —
  "sent" OTPs appear here once delivered. The gateway runs on the internal
  network and is served through the portal, so only one port is exposed.
- Database: Postgres, loaded from `seed/seed.sql` on first boot

Production server logs (Jan–Jun 2026) are provided in `logs/`.

Listen to `dc-briefing.mp3` first (the DC's briefing), then read `BRIEF.md`.
