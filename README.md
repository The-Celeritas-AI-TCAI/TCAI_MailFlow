# MailFlow

Copy `.env.example` to `.env`, add your Zoho SMTP credentials, then run `python app.py`.

Supabase is the primary recipient source. Keep its credentials in `.env` only:

```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-server-side-supabase-key
SUPABASE_TABLE=your_recipient_table
SUPABASE_EMAIL_COLUMN=email
# Optional extraction tuning
SUPABASE_PAGE_SIZE=500
SUPABASE_TIMEOUT=20
```

`SUPABASE_TABLE` and `SUPABASE_EMAIL_COLUMN` are intentionally required: MailFlow does not guess your database schema. The browser never receives the Supabase key. The legacy file importer remains available.

Campaigns can also automatically pause after a configured number of successful sends and resume after a configured number of minutes. Set both fields to `0` to disable automatic pauses. Retries are persisted with a next-attempt timestamp; waiting for a retry never occupies an SMTP worker.

Campaigns are persisted in SQLite. Sending uses a bounded worker pool, while the browser polls campaign status. Scheduled campaigns are stored in SQLite and checked every five seconds by the local scheduler process.

Optional email-signature settings in `.env`:

```
SMTP_USE_TLS=true
SMTP_TIMEOUT=30
MAX_EMAIL_RETRIES=3
RETRY_BASE_DELAY=1
SMTP_MAX_MESSAGE_SIZE=52428800
# Optional override; the bundled static/company-logo.png is used by default.
COMPANY_LOGO_PATH=C:/absolute/path/to/logo.png
SIGNATURE_NAME=Sayan Hati
SIGNATURE_TEAM=Team TCAI
SIGNATURE_PHONE=+91 70018 05069
SIGNATURE_WEBSITE=www.theceleritasai.com
```

The supplied Celeritas logo is bundled as `static/company-logo.png` and is used by default. `COMPANY_LOGO_PATH` is only needed to override it. MailFlow embeds the image as a CID inline image rather than a downloadable attachment. The configured SMTP message-size limit must include MIME/base64 overhead and must not exceed your Zoho plan's actual allowance.
