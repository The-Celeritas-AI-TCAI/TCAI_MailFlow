
"""
config.py
Loads application configuration from environment variables (.env file).
Credentials are NEVER hard-coded here.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Central configuration object for the Bulk Email Sender app."""

    # --- SMTP / Gmail settings ---
    SMTP_USERNAME = os.getenv("SMTP_USERNAME")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

    SMTP_SERVER = os.getenv("SMTP_SERVER")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))

    # --- Flask / upload settings ---
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
    MAX_CONTENT_LENGTH = int(
        os.environ.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
    )

    # --- File type restrictions ---
    ALLOWED_DATA_EXTENSIONS = {"csv", "xls", "xlsx", "pdf"}
    ALLOWED_ATTACHMENT_EXTENSIONS = {
        "pdf", "doc", "docx", "xls", "xlsx", "csv", "txt",
        "png", "jpg", "jpeg", "gif", "zip", "ppt", "pptx"
    }
    MAX_ATTACHMENT_SIZE = int(
        os.environ.get("MAX_ATTACHMENT_SIZE", 10 * 1024 * 1024)
    )

    @staticmethod
    def validate_smtp_config():
        """Raise a clear error if SMTP credentials are missing."""
        if not Config.SMTP_USERNAME or not Config.SMTP_PASSWORD:
            raise ValueError(
                "Email sending is not configured. Please set SMTP_USERNAME and "
                "SMTP_PASSWORD  in your .env file."
            )
