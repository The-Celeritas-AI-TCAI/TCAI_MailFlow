"""Environment-backed configuration for MailFlow."""
import os
import re
from dotenv import load_dotenv

load_dotenv()


# This is deliberately stricter than a MIME From header.  It protects the SMTP
# envelope (MAIL FROM), which must contain one bare mailbox -- never a display
# name, brackets, or a list of addresses.
_SMTP_ENVELOPE_ADDRESS = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
)


def normalized_smtp_username(value):
    """Return one safe MAIL FROM address or raise a non-secret config error."""
    if not isinstance(value, str):
        raise ValueError("SMTP_USERNAME must be a single email address.")
    username = value.strip()
    if (not username or any(char.isspace() for char in username)
            or any(char in username for char in "<>,;\r\n\"'")):
        raise ValueError("SMTP_USERNAME must be one bare email address without quotes, whitespace, or display-name syntax.")
    if not _SMTP_ENVELOPE_ADDRESS.fullmatch(username):
        raise ValueError("SMTP_USERNAME must be a valid single email address.")
    return username


class Config:
    SMTP_USERNAME = os.getenv("SMTP_USERNAME")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.zoho.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    # Bound SMTP DATA/response waits so a failed recipient cannot hold a worker indefinitely.
    SMTP_CONNECT_TIMEOUT = max(1, int(os.getenv("SMTP_CONNECT_TIMEOUT", "10")))
    SMTP_TIMEOUT = max(1, int(os.getenv("SMTP_TIMEOUT", "15")))
    # SMTP DATA for an attachment can legitimately exceed a text-message round
    # trip on slower uplinks; this applies only while transmitting the payload.
    SMTP_ATTACHMENT_TIMEOUT = max(SMTP_TIMEOUT, int(os.getenv("SMTP_ATTACHMENT_TIMEOUT", "45")))
    SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"}
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
    CAMPAIGN_ATTACHMENT_FOLDER = os.getenv("CAMPAIGN_ATTACHMENT_FOLDER", os.path.join(UPLOAD_FOLDER, "campaigns"))
    ATTACHMENT_RETENTION_SECONDS = max(0, int(os.getenv("ATTACHMENT_RETENTION_SECONDS", str(24 * 60 * 60))))
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(50 * 1024 * 1024)))
    MAX_CONTACT_FILE_SIZE = int(os.getenv("MAX_CONTACT_FILE_SIZE", str(10 * 1024 * 1024)))
    MAX_ATTACHMENT_SIZE = int(os.getenv("MAX_ATTACHMENT_SIZE", str(25 * 1024 * 1024)))
    MAX_TOTAL_ATTACHMENT_SIZE = int(os.getenv("MAX_TOTAL_ATTACHMENT_SIZE", str(40 * 1024 * 1024)))
    SMTP_MAX_MESSAGE_SIZE = int(os.getenv("SMTP_MAX_MESSAGE_SIZE", str(50 * 1024 * 1024)))
    # Zoho commonly rejects bursts of concurrent authenticated SMTP sessions.
    # One persistent worker is the reliable default; deployments may opt into up to three.
    EMAIL_WORKERS = min(3, max(1, int(os.getenv("EMAIL_WORKERS", "1"))))
    EMAIL_QUEUE_SIZE = max(EMAIL_WORKERS, int(os.getenv("EMAIL_QUEUE_SIZE", "500")))
    # This is the number of retries after the initial attempt, per recipient.
    MAX_EMAIL_RETRIES = max(0, int(os.getenv("MAX_EMAIL_RETRIES", "1")))
    RETRY_BASE_DELAY = max(0.1, float(os.getenv("RETRY_BASE_DELAY", "0.5")))
    EMAIL_RATE_LIMIT = max(0.0, float(os.getenv("EMAIL_RATE_LIMIT", "0")))
    SCHEDULER_TIMEZONE = os.getenv("SCHEDULER_TIMEZONE", "Asia/Kolkata")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///mailflow.db")
    COMPANY_LOGO_PATH = os.getenv(
        "COMPANY_LOGO_PATH",
        os.path.join(os.path.dirname(__file__), "static", "company-logo.png"),
    ).strip()
    COMPANY_LOGO_CID = "company_logo"
    SIGNATURE_NAME = os.getenv("SIGNATURE_NAME", "Sayan Hati")
    SIGNATURE_TEAM = os.getenv("SIGNATURE_TEAM", "Team TCAI")
    SIGNATURE_PHONE = os.getenv("SIGNATURE_PHONE", "+91 70018 05069")
    SIGNATURE_WEBSITE = os.getenv("SIGNATURE_WEBSITE", "www.theceleritasai.com")
    ALLOWED_DATA_EXTENSIONS = {"csv", "xls", "xlsx", "pdf"}
    ALLOWED_ATTACHMENT_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "csv", "txt", "png", "jpg", "jpeg", "gif", "zip", "ppt", "pptx"}

    @staticmethod
    def database_path():
        prefix = "sqlite:///"
        if not Config.DATABASE_URL.startswith(prefix):
            raise ValueError("This local deployment supports DATABASE_URL=sqlite:///path.db")
        return Config.DATABASE_URL[len(prefix):]

    @staticmethod
    def validate_smtp_config():
        if not Config.SMTP_USERNAME or not Config.SMTP_PASSWORD:
            raise ValueError("Email sending is not configured. Set SMTP_USERNAME and SMTP_PASSWORD in .env.")
        Config.smtp_username()

    @staticmethod
    def smtp_username():
        # Keep the raw value only long enough to normalize it.  Never log the
        # password, and never use this raw setting as a MAIL FROM argument.
        return normalized_smtp_username(Config.SMTP_USERNAME)
