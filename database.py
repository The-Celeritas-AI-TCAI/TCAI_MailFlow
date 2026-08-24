"""Small SQLite persistence layer; isolated so it can be replaced by an ORM later."""

import sqlite3
import uuid
import os
import shutil
from datetime import datetime, timezone

from config import Config

TERMINAL_CAMPAIGN_STATUSES = ("completed", "partially_failed", "failed", "cancelled")


def now():
    return datetime.now(timezone.utc).isoformat()


def connection():
    db = sqlite3.connect(Config.database_path(), timeout=30)
    db.row_factory = sqlite3.Row
    return db


def init_database():
    with connection() as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, scheduled_at TEXT, timezone TEXT, started_at TEXT, completed_at TEXT, subject TEXT NOT NULL, body TEXT NOT NULL, cancellation_requested INTEGER NOT NULL DEFAULT 0, fatal_error TEXT);
        CREATE TABLE IF NOT EXISTS recipients (id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL, email TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, sent_at TEXT, FOREIGN KEY(campaign_id) REFERENCES campaigns(id));
        CREATE INDEX IF NOT EXISTS idx_recipients_campaign_status ON recipients(campaign_id, status);
        CREATE TABLE IF NOT EXISTS attachments (id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL, filename TEXT NOT NULL, file_path TEXT NOT NULL, size INTEGER NOT NULL, mime_type TEXT, created_at TEXT NOT NULL, FOREIGN KEY(campaign_id) REFERENCES campaigns(id));
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(attachments)")}
        # Preserve historical campaigns without copying their BLOBs. New campaigns
        # use the metadata-only table created above; payload loading reads both.
        if "content" in columns:
            db.execute("ALTER TABLE attachments RENAME TO attachments_legacy")
            db.execute("CREATE TABLE attachments (id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL, filename TEXT NOT NULL, file_path TEXT NOT NULL, size INTEGER NOT NULL, mime_type TEXT, created_at TEXT NOT NULL, FOREIGN KEY(campaign_id) REFERENCES campaigns(id))")
        db.execute("CREATE INDEX IF NOT EXISTS idx_attachments_campaign ON attachments(campaign_id)")


def create_campaign(emails, subject, body, attachments, scheduled_at=None, timezone_name=None, name=None, campaign_id=None):
    campaign_id, created_at = campaign_id or str(uuid.uuid4()), now()
    with connection() as db:
        db.execute("INSERT INTO campaigns (id,name,status,created_at,scheduled_at,timezone,subject,body) VALUES (?,?,?,?,?,?,?,?)", (campaign_id, name or f"Campaign {created_at}", "scheduled" if scheduled_at else "queued", created_at, scheduled_at, timezone_name, subject, body))
        db.executemany("INSERT INTO recipients (campaign_id,email,status) VALUES (?,?,'queued')", [(campaign_id, email) for email in emails])
        db.executemany("INSERT INTO attachments (campaign_id,filename,file_path,size,mime_type,created_at) VALUES (?,?,?,?,?,?)", [(campaign_id, x["filename"], x["file_path"], x["size"], x.get("mime_type"), created_at) for x in attachments])
    return campaign_id


def get_campaign(campaign_id, include_failed=True):
    with connection() as db:
        row = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row: return None
        counts = dict(db.execute("SELECT status,COUNT(*) FROM recipients WHERE campaign_id=? GROUP BY status", (campaign_id,)).fetchall())
        attachment_list = [dict(x) for x in db.execute("SELECT id,filename,size FROM attachments WHERE campaign_id=?", (campaign_id,))]
        legacy_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments_legacy'").fetchone()
        if legacy_exists:
            attachment_list.extend(dict(x) for x in db.execute("SELECT id,filename,size FROM attachments_legacy WHERE campaign_id=?", (campaign_id,)))
        result = dict(row); result.update({"total": sum(counts.values()), "sent": counts.get("sent",0), "failed": counts.get("failed",0), "queued": counts.get("queued",0), "sending": counts.get("sending",0), "cancelled": counts.get("cancelled",0), "pending": counts.get("queued",0)+counts.get("sending",0), "attachments": attachment_list})
        if row["started_at"]:
            end = datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else datetime.now(timezone.utc)
            result["duration"] = round((end-datetime.fromisoformat(row["started_at"])).total_seconds(),2)
        else: result["duration"] = 0
        result["average_time_per_email"] = round(result["duration"]/result["sent"],2) if result["sent"] else 0
        if include_failed: result["failed_details"] = [dict(x) for x in db.execute("SELECT email,last_error,attempts FROM recipients WHERE campaign_id=? AND status='failed'", (campaign_id,))]
        return result


def list_campaigns():
    with connection() as db: ids = [x[0] for x in db.execute("SELECT id FROM campaigns ORDER BY created_at DESC")]
    return [get_campaign(x, False) for x in ids]


def activate_due_campaigns():
    with connection() as db:
        rows = db.execute("SELECT id FROM campaigns WHERE status='scheduled' AND scheduled_at <= ?", (now(),)).fetchall()
        db.executemany("UPDATE campaigns SET status='queued' WHERE id=? AND status='scheduled'", [(x[0],) for x in rows])
    return [x[0] for x in rows]


def recover_campaigns():
    with connection() as db:
        db.execute("UPDATE recipients SET status='queued' WHERE status='sending'")
        rows = db.execute("SELECT id FROM campaigns WHERE status IN ('queued','running')").fetchall()
        db.execute("UPDATE campaigns SET status='queued' WHERE status='running'")
    return [x[0] for x in rows]


def queued_recipient_ids(campaign_id):
    with connection() as db: return [x[0] for x in db.execute("SELECT id FROM recipients WHERE campaign_id=? AND status='queued' ORDER BY id", (campaign_id,))]


def claim_recipient(recipient_id):
    with connection() as db:
        row = db.execute("SELECT r.*,c.status campaign_status,c.cancellation_requested FROM recipients r JOIN campaigns c ON c.id=r.campaign_id WHERE r.id=?", (recipient_id,)).fetchone()
        if not row or row["status"] != "queued" or row["campaign_status"] not in ("queued","running") or row["cancellation_requested"]: return None
        db.execute("UPDATE recipients SET status='sending' WHERE id=? AND status='queued'", (recipient_id,))
        db.execute("UPDATE campaigns SET status='running',started_at=COALESCE(started_at,?) WHERE id=?", (now(),row["campaign_id"]))
        return dict(row)


def record_attempt(recipient_id):
    """Keep retry counts accurate without re-claiming the same recipient."""
    with connection() as db:
        db.execute("UPDATE recipients SET attempts=attempts+1 WHERE id=? AND status='sending'", (recipient_id,))


def campaign_payload(campaign_id):
    with connection() as db:
        campaign = dict(db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone())
        attachments = [dict(x) for x in db.execute("SELECT filename,file_path,size,mime_type FROM attachments WHERE campaign_id=?", (campaign_id,))]
        legacy_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments_legacy'").fetchone()
        if legacy_exists:
            attachments.extend(dict(x) for x in db.execute("SELECT filename,content,size FROM attachments_legacy WHERE campaign_id=?", (campaign_id,)))
    return campaign, attachments


def recipient_result(recipient_id, status, error=None):
    with connection() as db:
        db.execute("UPDATE recipients SET status=?,last_error=?,sent_at=? WHERE id=?", (status,error,now() if status == "sent" else None,recipient_id))
        campaign_id = db.execute("SELECT campaign_id FROM recipients WHERE id=?", (recipient_id,)).fetchone()[0]
    refresh_campaign_status(campaign_id)


def refresh_campaign_status(campaign_id):
    with connection() as db:
        campaign = db.execute("SELECT status,cancellation_requested FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not campaign or campaign["status"] == "failed": return
        counts = dict(db.execute("SELECT status,COUNT(*) FROM recipients WHERE campaign_id=? GROUP BY status", (campaign_id,)).fetchall()); pending = counts.get("queued",0)+counts.get("sending",0)
        if pending: status, complete = (("cancelled",now()) if campaign["cancellation_requested"] and not counts.get("sending",0) else ("running",None))
        elif campaign["cancellation_requested"]: status, complete = "cancelled",now()
        elif counts.get("failed",0) and counts.get("sent",0): status, complete = "partially_failed",now()
        elif counts.get("failed",0): status, complete = "failed",now()
        else: status, complete = "completed",now()
        db.execute("UPDATE campaigns SET status=?,completed_at=COALESCE(completed_at,?) WHERE id=?", (status,complete,campaign_id))


def cancel_campaign(campaign_id):
    with connection() as db:
        row = db.execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row: return None
        if row["status"] not in TERMINAL_CAMPAIGN_STATUSES:
            db.execute("UPDATE campaigns SET cancellation_requested=1 WHERE id=?", (campaign_id,)); db.execute("UPDATE recipients SET status='cancelled',last_error='Campaign cancelled' WHERE campaign_id=? AND status='queued'", (campaign_id,))
    refresh_campaign_status(campaign_id); return get_campaign(campaign_id)


def fail_campaign_authentication(campaign_id, error):
    fail_campaign_configuration(campaign_id, error)


def fail_campaign_configuration(campaign_id, error):
    """Terminal global SMTP configuration failure; do not retry per recipient."""
    with connection() as db:
        db.execute("UPDATE campaigns SET status='failed',fatal_error=?,completed_at=? WHERE id=?", (error,now(),campaign_id)); db.execute("UPDATE recipients SET status='failed',last_error=? WHERE campaign_id=? AND status IN ('queued','sending')", (error,campaign_id))


def retry_failed_campaign(campaign_id):
    with connection() as db:
        campaign = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone(); emails = [x[0] for x in db.execute("SELECT email FROM recipients WHERE campaign_id=? AND status='failed'", (campaign_id,))]; attachments = [dict(x) for x in db.execute("SELECT filename,file_path,size,mime_type FROM attachments WHERE campaign_id=?", (campaign_id,))]
    return create_campaign(emails,campaign["subject"],campaign["body"],attachments,name=f"Retry of {campaign_id}") if campaign and emails else None


def cleanup_expired_attachment_files(retention_seconds):
    """Delete only retained campaign files whose campaigns are already terminal."""
    cutoff = datetime.now(timezone.utc).timestamp() - retention_seconds
    with connection() as db:
        rows = db.execute("SELECT DISTINCT campaign_id,file_path FROM attachments JOIN campaigns ON campaigns.id=attachments.campaign_id WHERE campaigns.status IN ('completed','partially_failed','failed','cancelled')").fetchall()
    removed = 0
    for row in rows:
        path = row["file_path"]
        try:
            with connection() as db:
                still_needed = db.execute("SELECT 1 FROM attachments JOIN campaigns ON campaigns.id=attachments.campaign_id WHERE file_path=? AND campaigns.status NOT IN ('completed','partially_failed','failed','cancelled') LIMIT 1", (path,)).fetchone()
            if not still_needed and os.path.isfile(path) and os.path.getmtime(path) <= cutoff:
                os.remove(path); removed += 1
        except OSError:
            continue
    return removed


def delete_campaign(campaign_id):
    with connection() as db:
        if not db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone(): return False
        paths = [row[0] for row in db.execute("SELECT file_path FROM attachments WHERE campaign_id=?", (campaign_id,))]
        for table in ("attachments","recipients","campaigns"): db.execute(f"DELETE FROM {table} WHERE " + ("campaign_id" if table != "campaigns" else "id") + "=?", (campaign_id,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments_legacy'").fetchone():
            db.execute("DELETE FROM attachments_legacy WHERE campaign_id=?", (campaign_id,))
    for path in paths:
        try:
            with connection() as db:
                still_referenced = db.execute("SELECT 1 FROM attachments WHERE file_path=? LIMIT 1", (path,)).fetchone()
            if not still_referenced and os.path.isfile(path): os.remove(path)
        except OSError:
            continue
    return True
