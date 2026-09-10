"""Backend-only Supabase recipient extraction for MailFlow."""
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import Config

logger = logging.getLogger(__name__)


class SupabaseRecipientError(RuntimeError):
    """A safe, user-facing Supabase configuration or retrieval error."""


def fetch_recipient_emails(validate_email):
    """Page through the configured table and return normalized unique addresses.

    Supabase's REST endpoint is used deliberately: credentials remain server-side
    and no browser SDK/key is needed.  The table and column must be explicitly
    configured instead of guessing a production schema.
    """
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        raise SupabaseRecipientError("Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY in .env.")
    if not Config.SUPABASE_TABLE or not Config.SUPABASE_EMAIL_COLUMN:
        raise SupabaseRecipientError("Supabase recipient source is not configured. Set SUPABASE_TABLE and SUPABASE_EMAIL_COLUMN in .env.")
    base_url = Config.SUPABASE_URL.rstrip("/")
    endpoint = f"{base_url}/rest/v1/{Config.SUPABASE_TABLE}"
    headers = {"apikey": Config.SUPABASE_KEY, "Authorization": f"Bearer {Config.SUPABASE_KEY}", "Accept": "application/json"}
    offset, total_rows, invalid, duplicates = 0, 0, 0, 0
    emails, seen = [], set()
    logger.info("Supabase recipient fetch started (table=%s, column=%s)", Config.SUPABASE_TABLE, Config.SUPABASE_EMAIL_COLUMN)
    while True:
        query = urlencode({"select": Config.SUPABASE_EMAIL_COLUMN, "offset": offset, "limit": Config.SUPABASE_PAGE_SIZE})
        try:
            request = Request(f"{endpoint}?{query}", headers=headers)
            with urlopen(request, timeout=Config.SUPABASE_TIMEOUT) as response:
                rows = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            logger.warning("Supabase recipient fetch failed with HTTP status %s", exc.code)
            raise SupabaseRecipientError("Could not fetch recipients from Supabase. Verify its URL, key, table, and column configuration.") from exc
        except (URLError, TimeoutError, ValueError, OSError) as exc:
            logger.warning("Supabase recipient fetch failed: %s", type(exc).__name__)
            raise SupabaseRecipientError("Could not connect to Supabase. Check its configuration and network connection.") from exc
        if not isinstance(rows, list):
            raise SupabaseRecipientError("Supabase returned an unexpected recipient response.")
        total_rows += len(rows)
        for row in rows:
            value = row.get(Config.SUPABASE_EMAIL_COLUMN) if isinstance(row, dict) else None
            email = str(value).strip().lower() if value is not None else ""
            if not email:
                continue
            if not validate_email(email):
                invalid += 1
            elif email in seen:
                duplicates += 1
            else:
                seen.add(email); emails.append(email)
        if len(rows) < Config.SUPABASE_PAGE_SIZE:
            break
        offset += len(rows)
    if not emails:
        raise SupabaseRecipientError("Supabase returned no valid recipient emails; no campaign was created.")
    logger.info("Supabase recipient fetch completed: rows=%s valid=%s invalid=%s duplicates=%s", total_rows, len(emails), invalid, duplicates)
    return {"emails": emails, "total_records": total_rows, "valid_email_count": len(emails), "invalid_email_count": invalid, "duplicate_email_count": duplicates, "email_column": Config.SUPABASE_EMAIL_COLUMN}
