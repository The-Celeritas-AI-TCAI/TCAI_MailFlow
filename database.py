"""SQLite persistence layer for MailFlow.

The database is the authoritative source of truth for campaign/recipient
ownership and delivery state.

Important safety rule:
A recipient may only be claimed when BOTH:
    1. the recipient belongs to the requested campaign
    2. the recipient is still queued
"""

import os
import shutil
import sqlite3
import uuid

from datetime import datetime, timezone, timedelta

from config import Config


TERMINAL_CAMPAIGN_STATUSES = (
    "completed",
    "partially_failed",
    "failed",
    "cancelled",
)

ACTIVE_CAMPAIGN_STATUSES = (
    "queued",
    "running",
    "paused",
    "auto_paused",
    "resuming",
)


SENDABLE_CAMPAIGN_STATUSES = (
    "queued",
    "running",
    "resuming",
)


def now():
    """Return the current UTC timestamp as ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def connection():
    """Create a SQLite connection with row access by column name."""
    db = sqlite3.connect(
        Config.database_path(),
        timeout=30,
    )

    db.row_factory = sqlite3.Row

    # Foreign keys are connection-local in SQLite.
    # Enable them for every connection.
    db.execute("PRAGMA foreign_keys = ON")

    return db


def init_database():
    """Create/upgrade the MailFlow database schema safely."""

    with connection() as db:

        db.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                scheduled_at TEXT,
                timezone TEXT,
                started_at TEXT,
                completed_at TEXT,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                cancellation_requested INTEGER NOT NULL DEFAULT 0,
                fatal_error TEXT,
                automatic_pause_after INTEGER NOT NULL DEFAULT 0,
                automatic_pause_minutes INTEGER NOT NULL DEFAULT 0,
                next_resume_at TEXT,
                sent_since_pause INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL,
                email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                sent_at TEXT,
                next_attempt_at TEXT,
                FOREIGN KEY(campaign_id)
                    REFERENCES campaigns(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS
                idx_recipients_campaign_status
            ON recipients(campaign_id, status);

            CREATE INDEX IF NOT EXISTS
                idx_recipients_campaign_id
            ON recipients(campaign_id, id);

            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mime_type TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(campaign_id)
                    REFERENCES campaigns(id)
                    ON DELETE CASCADE
            );
            """
        )

        # --------------------------------------------------------------
        # CAMPAIGN MIGRATIONS
        # --------------------------------------------------------------

        campaign_columns = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(campaigns)"
            )
        }

        campaign_migrations = {
            "automatic_pause_after":
                "INTEGER NOT NULL DEFAULT 0",

            "automatic_pause_minutes":
                "INTEGER NOT NULL DEFAULT 0",

            "next_resume_at":
                "TEXT",

            "sent_since_pause":
                "INTEGER NOT NULL DEFAULT 0",
        }

        for column, definition in campaign_migrations.items():

            if column not in campaign_columns:

                db.execute(
                    f"ALTER TABLE campaigns "
                    f"ADD COLUMN {column} {definition}"
                )

        # --------------------------------------------------------------
        # RECIPIENT MIGRATIONS
        # --------------------------------------------------------------

        recipient_columns = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(recipients)"
            )
        }

        if "next_attempt_at" not in recipient_columns:

            db.execute(
                "ALTER TABLE recipients "
                "ADD COLUMN next_attempt_at TEXT"
            )

        # --------------------------------------------------------------
        # ATTACHMENT MIGRATION
        # --------------------------------------------------------------

        attachment_columns = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(attachments)"
            )
        }

        # Old versions stored attachment BLOB content directly in SQLite.
        # Preserve that table as attachments_legacy so existing historical
        # campaigns are not destroyed.
        if "content" in attachment_columns:

            legacy_exists = db.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table'
                  AND name='attachments_legacy'
                """
            ).fetchone()

            if not legacy_exists:

                db.execute(
                    "ALTER TABLE attachments "
                    "RENAME TO attachments_legacy"
                )

                db.execute(
                    """
                    CREATE TABLE attachments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        campaign_id TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        mime_type TEXT,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(campaign_id)
                            REFERENCES campaigns(id)
                            ON DELETE CASCADE
                    )
                    """
                )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_attachments_campaign
            ON attachments(campaign_id)
            """
        )


def create_campaign(
    emails,
    subject,
    body,
    attachments,
    scheduled_at=None,
    timezone_name=None,
    name=None,
    campaign_id=None,
    automatic_pause_after=0,
    automatic_pause_minutes=0,
):
    """
    Create a completely isolated campaign.

    Every recipient inserted here receives the NEW campaign_id.

    Existing recipients from previous campaigns are never reused.
    """

    campaign_id = (
        campaign_id
        or str(uuid.uuid4())
    )

    created_at = now()

    # --------------------------------------------------------------
    # NORMALIZE AND DEDUPLICATE EMAILS WITHIN THIS CAMPAIGN
    # --------------------------------------------------------------
    #
    # This does NOT remove an email merely because it existed in an
    # older campaign.
    #
    # It only prevents duplicate recipients inside THIS campaign.
    #
    normalized_emails = []

    seen = set()

    for email in emails or []:

        if not isinstance(email, str):
            continue

        email = email.strip()

        if not email:
            continue

        # Case-insensitive duplicate protection.
        email_key = email.casefold()

        if email_key in seen:
            continue

        seen.add(email_key)
        normalized_emails.append(email)

    with connection() as db:

        # ----------------------------------------------------------
        # CREATE CAMPAIGN
        # ----------------------------------------------------------

        db.execute(
            """
            INSERT INTO campaigns (
                id,
                name,
                status,
                created_at,
                scheduled_at,
                timezone,
                subject,
                body,
                automatic_pause_after,
                automatic_pause_minutes
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                campaign_id,
                name or f"Campaign {created_at}",
                "scheduled" if scheduled_at else "queued",
                created_at,
                scheduled_at,
                timezone_name,
                subject,
                body,
                automatic_pause_after,
                automatic_pause_minutes,
            ),
        )

        # ----------------------------------------------------------
        # CREATE RECIPIENT SNAPSHOT
        # ----------------------------------------------------------
        #
        # IMPORTANT:
        # These rows belong ONLY to this campaign.
        #
        # They are never selected from previous campaigns.
        # ----------------------------------------------------------

        if normalized_emails:

            db.executemany(
                """
                INSERT INTO recipients (
                    campaign_id,
                    email,
                    status
                )
                VALUES (
                    ?,
                    ?,
                    'queued'
                )
                """,
                [
                    (
                        campaign_id,
                        email,
                    )
                    for email in normalized_emails
                ],
            )

        # ----------------------------------------------------------
        # ATTACHMENTS
        # ----------------------------------------------------------

        if attachments:

            db.executemany(
                """
                INSERT INTO attachments (
                    campaign_id,
                    filename,
                    file_path,
                    size,
                    mime_type,
                    created_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        campaign_id,
                        item["filename"],
                        item["file_path"],
                        item["size"],
                        item.get("mime_type"),
                        created_at,
                    )
                    for item in attachments
                ],
            )

    return campaign_id


def get_campaign(
    campaign_id,
    include_failed=True,
):
    """Return campaign information and recipient statistics."""

    with connection() as db:

        row = db.execute(
            """
            SELECT *
            FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        ).fetchone()

        if not row:
            return None

        counts = dict(
            db.execute(
                """
                SELECT status, COUNT(*)
                FROM recipients
                WHERE campaign_id=?
                GROUP BY status
                """,
                (campaign_id,),
            ).fetchall()
        )

        attachment_list = [
            dict(x)
            for x in db.execute(
                """
                SELECT
                    id,
                    filename,
                    size
                FROM attachments
                WHERE campaign_id=?
                """,
                (campaign_id,),
            )
        ]

        legacy_exists = db.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table'
              AND name='attachments_legacy'
            """
        ).fetchone()

        if legacy_exists:

            attachment_list.extend(
                dict(x)
                for x in db.execute(
                    """
                    SELECT
                        id,
                        filename,
                        size
                    FROM attachments_legacy
                    WHERE campaign_id=?
                    """,
                    (campaign_id,),
                )
            )

        result = dict(row)

        result.update(
            {
                "total": sum(counts.values()),
                "sent": counts.get("sent", 0),
                "failed": counts.get("failed", 0),
                "queued": counts.get("queued", 0),
                "retrying": counts.get("retrying", 0),
                "sending": counts.get("sending", 0),
                "cancelled": counts.get("cancelled", 0),
                "pending": (
                    counts.get("queued", 0)
                    + counts.get("retrying", 0)
                    + counts.get("sending", 0)
                ),
                "attachments": attachment_list,
            }
        )

        if row["started_at"]:

            end = (
                datetime.fromisoformat(
                    row["completed_at"]
                )
                if row["completed_at"]
                else datetime.now(timezone.utc)
            )

            result["duration"] = round(
                (
                    end
                    - datetime.fromisoformat(
                        row["started_at"]
                    )
                ).total_seconds(),
                2,
            )

        else:
            result["duration"] = 0

        result["average_time_per_email"] = (
            round(
                result["duration"]
                / result["sent"],
                2,
            )
            if result["sent"]
            else 0
        )

        if include_failed:

            result["failed_details"] = [
                dict(x)
                for x in db.execute(
                    """
                    SELECT
                        email,
                        last_error,
                        attempts
                    FROM recipients
                    WHERE campaign_id=?
                      AND status='failed'
                    """,
                    (campaign_id,),
                )
            ]

        return result


def list_campaigns():

    with connection() as db:

        ids = [
            x[0]
            for x in db.execute(
                """
                SELECT id
                FROM campaigns
                ORDER BY created_at DESC
                """
            )
        ]

    return [
        get_campaign(campaign_id, False)
        for campaign_id in ids
    ]


def activate_due_campaigns():
    """Move scheduled campaigns into queued state."""

    with connection() as db:

        rows = db.execute(
            """
            SELECT id
            FROM campaigns
            WHERE status='scheduled'
              AND scheduled_at <= ?
            """,
            (now(),),
        ).fetchall()

        if rows:

            db.executemany(
                """
                UPDATE campaigns
                SET status='queued'
                WHERE id=?
                  AND status='scheduled'
                """,
                [
                    (row[0],)
                    for row in rows
                ],
            )

    return [
        row[0]
        for row in rows
    ]


def recover_campaigns():
    """
    Recover interrupted campaigns after process restart.

    IMPORTANT:
    Restart does NOT authorize sending.

    Interrupted campaigns become paused.
    Recipients that were being sent return to queued state.
    """

    with connection() as db:

        # Only reset recipients that were actually in-flight.
        db.execute(
            """
            UPDATE recipients
            SET
                status='queued',
                next_attempt_at=NULL
            WHERE status='sending'
              AND campaign_id IN (
                  SELECT id
                  FROM campaigns
                  WHERE status IN (
                      'queued',
                      'running',
                      'resuming',
                      'auto_paused'
                  )
              )
            """
        )

        rows = db.execute(
            """
            SELECT id
            FROM campaigns
            WHERE status IN (
                'queued',
                'running',
                'resuming',
                'auto_paused'
            )
            """
        ).fetchall()

        db.execute(
            """
            UPDATE campaigns
            SET
                status='paused',
                next_resume_at=NULL
            WHERE status IN (
                'queued',
                'running',
                'resuming',
                'auto_paused'
            )
            """
        )

    return [
        row[0]
        for row in rows
    ]


def queued_recipient_ids(campaign_id):
    """
    Return ONLY queued recipient IDs belonging to campaign_id.

    This is intentionally campaign-scoped.
    """

    if not campaign_id:
        return []

    with connection() as db:

        rows = db.execute(
            """
            SELECT r.id
            FROM recipients AS r
            INNER JOIN campaigns AS c
                ON c.id = r.campaign_id
            WHERE r.campaign_id=?
              AND r.status='queued'
              AND c.status IN (
                  'queued',
                  'running',
                  'resuming'
              )
              AND c.cancellation_requested=0
            ORDER BY r.id
            """,
            (campaign_id,),
        ).fetchall()

    return [
        row[0]
        for row in rows
    ]


def claim_recipient(
    recipient_id,
    campaign_id=None,
):
    """
    Atomically claim a recipient.

    SECURITY/CONSISTENCY RULE:

    If campaign_id is supplied, the recipient MUST belong to that campaign.

    This prevents a stale/inadvertent queue entry from sending a recipient
    belonging to another campaign.

    campaign_id should always be supplied by the worker.
    """

    if not recipient_id:
        return None

    if not campaign_id:
        # Backward compatibility only.
        #
        # New worker code should ALWAYS pass campaign_id.
        logger_warning = (
            "claim_recipient called without campaign_id; "
            "update email_worker.py to pass campaign_id"
        )
        print(f"[DATABASE WARNING] {logger_warning}")

    with connection() as db:

        db.execute("BEGIN IMMEDIATE")

        # ----------------------------------------------------------
        # AUTHORITATIVE OWNERSHIP CHECK
        # ----------------------------------------------------------

        if campaign_id:

            row = db.execute(
                """
                SELECT
                    r.*,
                    c.status AS campaign_status,
                    c.cancellation_requested
                FROM recipients AS r
                INNER JOIN campaigns AS c
                    ON c.id = r.campaign_id
                WHERE r.id=?
                  AND r.campaign_id=?
                """,
                (
                    recipient_id,
                    campaign_id,
                ),
            ).fetchone()

        else:

            row = db.execute(
                """
                SELECT
                    r.*,
                    c.status AS campaign_status,
                    c.cancellation_requested
                FROM recipients AS r
                INNER JOIN campaigns AS c
                    ON c.id = r.campaign_id
                WHERE r.id=?
                """,
                (recipient_id,),
            ).fetchone()

        # ----------------------------------------------------------
        # VALIDATION
        # ----------------------------------------------------------

        if not row:
            return None

        if row["status"] != "queued":
            return None

        if row["campaign_status"] not in SENDABLE_CAMPAIGN_STATUSES:
            return None

        if row["cancellation_requested"]:
            return None

        # ----------------------------------------------------------
        # ATOMIC CLAIM
        # ----------------------------------------------------------

        updated = db.execute(
            """
            UPDATE recipients
            SET
                status='sending',
                next_attempt_at=NULL
            WHERE id=?
              AND campaign_id=?
              AND status='queued'
            """,
            (
                recipient_id,
                row["campaign_id"],
            ),
        ).rowcount

        if updated != 1:
            return None

        # ----------------------------------------------------------
        # MARK CAMPAIGN RUNNING
        # ----------------------------------------------------------

        db.execute(
            """
            UPDATE campaigns
            SET
                status='running',
                started_at=COALESCE(
                    started_at,
                    ?
                )
            WHERE id=?
              AND status IN (
                  'queued',
                  'resuming'
              )
            """,
            (
                now(),
                row["campaign_id"],
            ),
        )

        return dict(row)


def record_attempt(recipient_id):
    """Increment the recipient attempt counter."""

    with connection() as db:

        db.execute(
            """
            UPDATE recipients
            SET attempts=attempts+1
            WHERE id=?
              AND status='sending'
            """,
            (recipient_id,),
        )

        row = db.execute(
            """
            SELECT attempts
            FROM recipients
            WHERE id=?
            """,
            (recipient_id,),
        ).fetchone()

    return (
        row["attempts"]
        if row
        else 0
    )


def campaign_payload(campaign_id):
    """Return campaign data and ONLY its attachments."""

    with connection() as db:

        campaign_row = db.execute(
            """
            SELECT *
            FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        ).fetchone()

        if not campaign_row:
            raise ValueError(
                f"Campaign not found: {campaign_id}"
            )

        campaign = dict(campaign_row)

        attachments = [
            dict(x)
            for x in db.execute(
                """
                SELECT
                    filename,
                    file_path,
                    size,
                    mime_type
                FROM attachments
                WHERE campaign_id=?
                ORDER BY id
                """,
                (campaign_id,),
            )
        ]

        legacy_exists = db.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table'
              AND name='attachments_legacy'
            """
        ).fetchone()

        if legacy_exists:

            attachments.extend(
                dict(x)
                for x in db.execute(
                    """
                    SELECT
                        filename,
                        content,
                        size
                    FROM attachments_legacy
                    WHERE campaign_id=?
                    ORDER BY id
                    """,
                    (campaign_id,),
                )
            )

    return campaign, attachments


def recipient_result(
    recipient_id,
    status,
    error=None,
):
    """Record final result for a recipient."""

    with connection() as db:

        db.execute(
            """
            UPDATE recipients
            SET
                status=?,
                last_error=?,
                sent_at=?,
                next_attempt_at=NULL
            WHERE id=?
            """,
            (
                status,
                error,
                now() if status == "sent" else None,
                recipient_id,
            ),
        )

        row = db.execute(
            """
            SELECT campaign_id
            FROM recipients
            WHERE id=?
            """,
            (recipient_id,),
        ).fetchone()

        if not row:
            return

        campaign_id = row["campaign_id"]

    if status == "sent":
        _maybe_start_automatic_pause(
            campaign_id
        )

    refresh_campaign_status(
        campaign_id
    )


def schedule_retry(
    recipient_id,
    error,
    delay_seconds,
):
    """Move a sending recipient into retrying state."""

    retry_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=delay_seconds)
    ).isoformat()

    with connection() as db:

        row = db.execute(
            """
            SELECT campaign_id
            FROM recipients
            WHERE id=?
              AND status='sending'
            """,
            (recipient_id,),
        ).fetchone()

        if not row:
            return

        db.execute(
            """
            UPDATE recipients
            SET
                status='retrying',
                last_error=?,
                next_attempt_at=?
            WHERE id=?
              AND status='sending'
            """,
            (
                error,
                retry_at,
                recipient_id,
            ),
        )

        campaign_id = row["campaign_id"]

    refresh_campaign_status(
        campaign_id
    )


def _maybe_start_automatic_pause(
    campaign_id,
):
    """Apply automatic pause rules after successful delivery."""

    with connection() as db:

        db.execute("BEGIN IMMEDIATE")

        campaign = db.execute(
            """
            SELECT
                status,
                automatic_pause_after,
                automatic_pause_minutes,
                sent_since_pause,
                cancellation_requested
            FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        ).fetchone()

        if not campaign:
            return

        if (
            campaign["status"]
            not in ("running", "queued")
            or campaign["cancellation_requested"]
        ):
            return

        after = campaign[
            "automatic_pause_after"
        ]

        minutes = campaign[
            "automatic_pause_minutes"
        ]

        sent_count = (
            campaign["sent_since_pause"]
            + 1
        )

        if (
            after > 0
            and minutes > 0
            and sent_count >= after
        ):

            resume_at = (
                datetime.now(timezone.utc)
                + timedelta(minutes=minutes)
            ).isoformat()

            db.execute(
                """
                UPDATE campaigns
                SET
                    status='auto_paused',
                    next_resume_at=?,
                    sent_since_pause=0
                WHERE id=?
                """,
                (
                    resume_at,
                    campaign_id,
                ),
            )

        else:

            db.execute(
                """
                UPDATE campaigns
                SET sent_since_pause=?
                WHERE id=?
                """,
                (
                    sent_count,
                    campaign_id,
                ),
            )


def refresh_campaign_status(
    campaign_id,
):
    """Recalculate campaign status from recipient state."""

    with connection() as db:

        campaign = db.execute(
            """
            SELECT
                status,
                cancellation_requested
            FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        ).fetchone()

        if not campaign:
            return

        if campaign["status"] == "failed":
            return

        counts = dict(
            db.execute(
                """
                SELECT
                    status,
                    COUNT(*)
                FROM recipients
                WHERE campaign_id=?
                GROUP BY status
                """,
                (campaign_id,),
            ).fetchall()
        )

        pending = (
            counts.get("queued", 0)
            + counts.get("retrying", 0)
            + counts.get("sending", 0)
        )

        if pending:

            if (
                campaign["cancellation_requested"]
                and not counts.get(
                    "sending",
                    0,
                )
            ):
                status = "cancelled"
                complete = now()

            elif campaign["status"] in (
                "paused",
                "auto_paused",
            ):
                status = campaign["status"]
                complete = None

            else:
                status = "running"
                complete = None

        elif campaign["cancellation_requested"]:

            status = "cancelled"
            complete = now()

        elif (
            counts.get("failed", 0)
            and counts.get("sent", 0)
        ):

            status = "partially_failed"
            complete = now()

        elif counts.get("failed", 0):

            status = "failed"
            complete = now()

        else:

            status = "completed"
            complete = now()

        db.execute(
            """
            UPDATE campaigns
            SET
                status=?,
                completed_at=COALESCE(
                    completed_at,
                    ?
                )
            WHERE id=?
            """,
            (
                status,
                complete,
                campaign_id,
            ),
        )


def cancel_campaign(
    campaign_id,
):
    """Cancel a campaign and its unsent recipients."""

    with connection() as db:

        row = db.execute(
            """
            SELECT status
            FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        ).fetchone()

        if not row:
            return None

        if row["status"] not in TERMINAL_CAMPAIGN_STATUSES:

            db.execute(
                """
                UPDATE campaigns
                SET
                    cancellation_requested=1,
                    next_resume_at=NULL
                WHERE id=?
                """,
                (campaign_id,),
            )

            db.execute(
                """
                UPDATE recipients
                SET
                    status='cancelled',
                    last_error='Campaign cancelled',
                    next_attempt_at=NULL
                WHERE campaign_id=?
                  AND status IN (
                      'queued',
                      'retrying'
                  )
                """,
                (campaign_id,),
            )

    refresh_campaign_status(
        campaign_id
    )

    return get_campaign(
        campaign_id
    )


def fail_campaign_authentication(
    campaign_id,
    error,
):
    """Backward-compatible alias."""
    fail_campaign_configuration(
        campaign_id,
        error,
    )


def fail_campaign_configuration(
    campaign_id,
    error,
):
    """
    Terminal campaign-wide SMTP configuration failure.
    """

    with connection() as db:

        db.execute(
            """
            UPDATE campaigns
            SET
                status='failed',
                fatal_error=?,
                completed_at=?
            WHERE id=?
            """,
            (
                error,
                now(),
                campaign_id,
            ),
        )

        db.execute(
            """
            UPDATE recipients
            SET
                status='failed',
                last_error=?,
                next_attempt_at=NULL
            WHERE campaign_id=?
              AND status IN (
                  'queued',
                  'sending',
                  'retrying'
              )
            """,
            (
                error,
                campaign_id,
            ),
        )


def retry_failed_campaign(
    campaign_id,
):
    """
    Create a NEW campaign containing only recipients that failed
    in the selected campaign.

    This intentionally creates NEW recipient rows with a NEW campaign_id.
    """

    with connection() as db:

        campaign_row = db.execute(
            """
            SELECT *
            FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        ).fetchone()

        if not campaign_row:
            return None

        failed_emails = [
            row[0]
            for row in db.execute(
                """
                SELECT email
                FROM recipients
                WHERE campaign_id=?
                  AND status='failed'
                ORDER BY id
                """,
                (campaign_id,),
            )
        ]

        attachments = [
            dict(row)
            for row in db.execute(
                """
                SELECT
                    filename,
                    file_path,
                    size,
                    mime_type
                FROM attachments
                WHERE campaign_id=?
                ORDER BY id
                """,
                (campaign_id,),
            )
        ]

    if not failed_emails:
        return None

    return create_campaign(
        failed_emails,
        campaign_row["subject"],
        campaign_row["body"],
        attachments,
        name=f"Retry of {campaign_id}",
    )


def pause_campaign(
    campaign_id,
):
    """Pause a campaign."""

    with connection() as db:

        row = db.execute(
            """
            SELECT status
            FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        ).fetchone()

        if not row:
            return None

        if row["status"] in (
            "queued",
            "running",
            "resuming",
            "auto_paused",
        ):

            db.execute(
                """
                UPDATE campaigns
                SET
                    status='paused',
                    next_resume_at=NULL
                WHERE id=?
                """,
                (campaign_id,),
            )

    return get_campaign(
        campaign_id
    )


def resume_campaign(
    campaign_id,
):
    """Resume a paused campaign."""

    with connection() as db:

        row = db.execute(
            """
            SELECT
                status,
                cancellation_requested
            FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        ).fetchone()

        if not row:
            return None

        if (
            row["status"]
            in ("paused", "auto_paused")
            and not row["cancellation_requested"]
        ):

            db.execute(
                """
                UPDATE campaigns
                SET
                    status='resuming',
                    next_resume_at=NULL
                WHERE id=?
                """,
                (campaign_id,),
            )

    return get_campaign(
        campaign_id
    )


def activate_due_retries_and_resumes():
    """
    Activate due retries and automatic resumes.

    Returns campaign IDs that have queued recipients.
    """

    current_time = now()

    with connection() as db:

        # ----------------------------------------------------------
        # RETRIES
        # ----------------------------------------------------------

        db.execute(
            """
            UPDATE recipients
            SET
                status='queued',
                next_attempt_at=NULL
            WHERE status='retrying'
              AND next_attempt_at <= ?
              AND campaign_id IN (
                  SELECT id
                  FROM campaigns
                  WHERE status IN (
                      'queued',
                      'running',
                      'resuming'
                  )
                    AND cancellation_requested=0
              )
            """,
            (current_time,),
        )

        # ----------------------------------------------------------
        # AUTOMATIC RESUME
        # ----------------------------------------------------------

        rows = db.execute(
            """
            SELECT id
            FROM campaigns
            WHERE status='auto_paused'
              AND next_resume_at <= ?
              AND cancellation_requested=0
            """,
            (current_time,),
        ).fetchall()

        if rows:

            db.executemany(
                """
                UPDATE campaigns
                SET
                    status='resuming',
                    next_resume_at=NULL
                WHERE id=?
                  AND status='auto_paused'
                """,
                [
                    (row[0],)
                    for row in rows
                ],
            )

        # ----------------------------------------------------------
        # FIND CAMPAIGNS WITH QUEUED RECIPIENTS
        # ----------------------------------------------------------

        active = db.execute(
            """
            SELECT DISTINCT c.id
            FROM campaigns AS c
            INNER JOIN recipients AS r
                ON r.campaign_id = c.id
            WHERE c.status IN (
                'queued',
                'running',
                'resuming'
            )
              AND c.cancellation_requested=0
              AND r.status='queued'
            """
        ).fetchall()

    campaign_ids = {
        row[0]
        for row in rows
    }

    campaign_ids.update(
        row[0]
        for row in active
    )

    return list(campaign_ids)


def cleanup_expired_attachment_files(
    retention_seconds,
):
    """
    Delete retained campaign files only when their campaigns are terminal.
    """

    cutoff = (
        datetime.now(timezone.utc).timestamp()
        - retention_seconds
    )

    with connection() as db:

        rows = db.execute(
            """
            SELECT DISTINCT
                a.campaign_id,
                a.file_path
            FROM attachments AS a
            INNER JOIN campaigns AS c
                ON c.id=a.campaign_id
            WHERE c.status IN (
                'completed',
                'partially_failed',
                'failed',
                'cancelled'
            )
            """
        ).fetchall()

    removed = 0

    for row in rows:

        path = row["file_path"]

        try:

            with connection() as db:

                still_needed = db.execute(
                    """
                    SELECT 1
                    FROM attachments AS a
                    INNER JOIN campaigns AS c
                        ON c.id=a.campaign_id
                    WHERE a.file_path=?
                      AND c.status NOT IN (
                          'completed',
                          'partially_failed',
                          'failed',
                          'cancelled'
                      )
                    LIMIT 1
                    """,
                    (path,),
                ).fetchone()

            if (
                not still_needed
                and os.path.isfile(path)
                and os.path.getmtime(path)
                <= cutoff
            ):

                os.remove(path)
                removed += 1

        except OSError:
            continue

    return removed


def delete_campaign(
    campaign_id,
):
    """Delete a campaign and its recipient/attachment records."""

    with connection() as db:

        if not db.execute(
            """
            SELECT 1
            FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        ).fetchone():

            return False

        paths = [
            row[0]
            for row in db.execute(
                """
                SELECT file_path
                FROM attachments
                WHERE campaign_id=?
                """,
                (campaign_id,),
            )
        ]

        # ----------------------------------------------------------
        # Delete dependent records first.
        # ----------------------------------------------------------

        db.execute(
            """
            DELETE FROM attachments
            WHERE campaign_id=?
            """,
            (campaign_id,),
        )

        db.execute(
            """
            DELETE FROM recipients
            WHERE campaign_id=?
            """,
            (campaign_id,),
        )

        db.execute(
            """
            DELETE FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        )

        legacy_exists = db.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table'
              AND name='attachments_legacy'
            """
        ).fetchone()

        if legacy_exists:

            db.execute(
                """
                DELETE FROM attachments_legacy
                WHERE campaign_id=?
                """,
                (campaign_id,),
            )

    # --------------------------------------------------------------
    # Remove files only when no other campaign references them.
    # --------------------------------------------------------------

    for path in paths:

        try:

            with connection() as db:

                still_referenced = db.execute(
                    """
                    SELECT 1
                    FROM attachments
                    WHERE file_path=?
                    LIMIT 1
                    """,
                    (path,),
                ).fetchone()

            if (
                not still_referenced
                and os.path.isfile(path)
            ):
                os.remove(path)

        except OSError:
            continue

    return True