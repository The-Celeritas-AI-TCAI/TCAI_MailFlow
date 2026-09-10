"""Environment-backed configuration for MailFlow."""

import os
import re
from dotenv import load_dotenv

load_dotenv()


# This is deliberately stricter than a MIME From header. It protects the SMTP
# envelope (MAIL FROM), which must contain one bare mailbox.
_SMTP_ENVELOPE_ADDRESS = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
)


def normalized_smtp_username(value):
    """Return one safe MAIL FROM address or raise a non-secret config error."""
    if not isinstance(value, str):
        raise ValueError("SMTP_USERNAME must be a single email address.")

    username = value.strip()

    if (
        not username
        or any(char.isspace() for char in username)
        or any(char in "<>,;\r\n\"'" for char in username)
    ):
        raise ValueError(
            "SMTP_USERNAME must be one bare email address "
            "without quotes, whitespace, or display-name syntax."
        )

    if not _SMTP_ENVELOPE_ADDRESS.fullmatch(username):
        raise ValueError("SMTP_USERNAME must be a valid single email address.")

    return username


class Config:
    # ------------------------------------------------------------------
    # SUPABASE
    # ------------------------------------------------------------------
    SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
    SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
    SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "").strip()
    SUPABASE_EMAIL_COLUMN = os.getenv("SUPABASE_EMAIL_COLUMN", "").strip()

    SUPABASE_PAGE_SIZE = min(
        1000,
        max(1, int(os.getenv("SUPABASE_PAGE_SIZE", "500"))),
    )

    SUPABASE_TIMEOUT = max(
        1,
        int(os.getenv("SUPABASE_TIMEOUT", "20")),
    )

    # ------------------------------------------------------------------
    # SMTP
    # ------------------------------------------------------------------
    SMTP_USERNAME = os.getenv("SMTP_USERNAME")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

    SMTP_SERVER = os.getenv(
        "SMTP_SERVER",
        "smtp.zoho.com",
    )

    SMTP_PORT = int(
        os.getenv("SMTP_PORT", "587")
    )

    SMTP_USE_TLS = (
        os.getenv("SMTP_USE_TLS", "true")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )

    # Connection establishment timeout.
    # This only applies to connecting to the SMTP server.
    SMTP_CONNECT_TIMEOUT = max(
        5,
        int(os.getenv("SMTP_CONNECT_TIMEOUT", "20")),
    )

    # Normal SMTP command/response timeout.
    # Used for EHLO, MAIL FROM, RCPT TO, etc.
    SMTP_TIMEOUT = max(
        15,
        int(os.getenv("SMTP_TIMEOUT", "30")),
    )

    # SMTP DATA timeout.
    #
    # IMPORTANT:
    # This is an IDLE timeout, not an absolute maximum campaign/message time.
    # ResilientSMTP renews the deadline whenever bytes are successfully written.
    #
    # This allows larger messages or slower temporary network conditions
    # without killing a healthy transfer.
    SMTP_ATTACHMENT_TIMEOUT = max(
        SMTP_TIMEOUT,
        int(os.getenv("SMTP_ATTACHMENT_TIMEOUT", "300")),
    )

    # Maximum generated MIME message size.
    SMTP_MAX_MESSAGE_SIZE = int(
        os.getenv(
            "SMTP_MAX_MESSAGE_SIZE",
            str(50 * 1024 * 1024),
        )
    )

    # ------------------------------------------------------------------
    # APPLICATION / UPLOADS
    # ------------------------------------------------------------------
    SECRET_KEY = os.getenv(
        "SECRET_KEY",
        "dev-secret-key-change-in-production",
    )

    UPLOAD_FOLDER = os.getenv(
        "UPLOAD_FOLDER",
        "uploads",
    )

    CAMPAIGN_ATTACHMENT_FOLDER = os.getenv(
        "CAMPAIGN_ATTACHMENT_FOLDER",
        os.path.join(UPLOAD_FOLDER, "campaigns"),
    )

    ATTACHMENT_RETENTION_SECONDS = max(
        0,
        int(
            os.getenv(
                "ATTACHMENT_RETENTION_SECONDS",
                str(24 * 60 * 60),
            )
        ),
    )

    MAX_CONTENT_LENGTH = int(
        os.getenv(
            "MAX_CONTENT_LENGTH",
            str(50 * 1024 * 1024),
        )
    )

    MAX_CONTACT_FILE_SIZE = int(
        os.getenv(
            "MAX_CONTACT_FILE_SIZE",
            str(10 * 1024 * 1024),
        )
    )

    MAX_ATTACHMENT_SIZE = int(
        os.getenv(
            "MAX_ATTACHMENT_SIZE",
            str(25 * 1024 * 1024),
        )
    )

    MAX_TOTAL_ATTACHMENT_SIZE = int(
        os.getenv(
            "MAX_TOTAL_ATTACHMENT_SIZE",
            str(40 * 1024 * 1024),
        )
    )

    # ------------------------------------------------------------------
    # WORKERS / QUEUES
    # ------------------------------------------------------------------
    # Zoho can throttle concurrent authenticated SMTP sessions.
    # Keep 1 as the safe default, but allow up to 3.
    EMAIL_WORKERS = min(
        3,
        max(
            1,
            int(os.getenv("EMAIL_WORKERS", "1")),
        ),
    )

    EMAIL_QUEUE_SIZE = max(
        EMAIL_WORKERS,
        int(os.getenv("EMAIL_QUEUE_SIZE", "500")),
    )

    # Number of retries AFTER the initial attempt.
    MAX_EMAIL_RETRIES = max(
        0,
        int(os.getenv("MAX_EMAIL_RETRIES", "2")),
    )

    # Initial retry delay.
    RETRY_BASE_DELAY = max(
        1.0,
        float(os.getenv("RETRY_BASE_DELAY", "5")),
    )

    # Optional global send rate limit.
    EMAIL_RATE_LIMIT = max(
        0.0,
        float(os.getenv("EMAIL_RATE_LIMIT", "0")),
    )

    # ------------------------------------------------------------------
    # SCHEDULER / DATABASE
    # ------------------------------------------------------------------
    SCHEDULER_TIMEZONE = os.getenv(
        "SCHEDULER_TIMEZONE",
        "Asia/Kolkata",
    )

    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "sqlite:///mailflow.db",
    )

    # ------------------------------------------------------------------
    # COMPANY SIGNATURE
    # ------------------------------------------------------------------
    COMPANY_LOGO_PATH = os.getenv(
        "COMPANY_LOGO_PATH",
        os.path.join(
            os.path.dirname(__file__),
            "static",
            "company-logo.png",
        ),
    ).strip()

    COMPANY_LOGO_CID = "company_logo"

    SIGNATURE_NAME = os.getenv(
        "SIGNATURE_NAME",
        "Sayan Hati",
    )

    SIGNATURE_TEAM = os.getenv(
        "SIGNATURE_TEAM",
        "Team TCAI",
    )

    SIGNATURE_PHONE = os.getenv(
        "SIGNATURE_PHONE",
        "+91 70018 05069",
    )

    SIGNATURE_WEBSITE = os.getenv(
        "SIGNATURE_WEBSITE",
        "www.theceleritasai.com",
    )

    # ------------------------------------------------------------------
    # FILE TYPES
    # ------------------------------------------------------------------
    ALLOWED_DATA_EXTENSIONS = {
        "csv",
        "xls",
        "xlsx",
        "pdf",
    }

    ALLOWED_ATTACHMENT_EXTENSIONS = {
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "csv",
        "txt",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "zip",
        "ppt",
        "pptx",
    }

    # ------------------------------------------------------------------
    # DATABASE HELPERS
    # ------------------------------------------------------------------
    @staticmethod
    def database_path():
        prefix = "sqlite:///"

        if not Config.DATABASE_URL.startswith(prefix):
            raise ValueError(
                "This local deployment supports "
                "DATABASE_URL=sqlite:///path.db"
            )

        return Config.DATABASE_URL[len(prefix):]

    # ------------------------------------------------------------------
    # SMTP VALIDATION
    # ------------------------------------------------------------------
    @staticmethod
    def validate_smtp_config():
        if not Config.SMTP_USERNAME or not Config.SMTP_PASSWORD:
            raise ValueError(
                "Email sending is not configured. "
                "Set SMTP_USERNAME and SMTP_PASSWORD in .env."
            )

        Config.smtp_username()

    @staticmethod
    def smtp_username():
        # Keep the raw value only long enough to normalize it.
        # Never log the password.
        return normalized_smtp_username(Config.SMTP_USERNAME)