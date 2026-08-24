import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pdfplumber
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

import database
from config import Config
from email_worker import campaign_manager, estimate_message_size

app = Flask(__name__)
app.config.from_object(Config)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(Config.CAMPAIGN_ATTACHMENT_FOLDER, exist_ok=True)
database.init_database()
campaign_manager.start()

EMAIL_REGEX = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")


def allowed_data_file(filename):
    return bool(filename and "." in filename and filename.rsplit(".", 1)[1].lower() in Config.ALLOWED_DATA_EXTENSIONS)


def allowed_attachment_file(filename):
    return bool(filename and "." in filename and filename.rsplit(".", 1)[1].lower() in Config.ALLOWED_ATTACHMENT_EXTENSIONS)


def validate_email(email):
    if not isinstance(email, str) or not EMAIL_REGEX.match(email.strip().lower()):
        return False
    return email.strip().lower().split("@", 1)[1] not in {"example.com", "example.org", "example.net", "txt.com"}


def normalize_column_name(column):
    return re.sub(r"[^a-z0-9]", "", str(column).strip().lower())


def find_email_column(columns):
    normalized = {normalize_column_name(column): column for column in columns}
    for key, value in normalized.items():
        if key in {"email", "emailaddress", "emailid", "mail"}: return value
    return next((value for key, value in normalized.items() if "email" in key), None)


def clean_dataframe(df):
    df = df.copy(); df.columns = [str(column).strip() for column in df.columns]
    return df.dropna(how="all")


def parse_data_file(path, filename):
    extension = filename.rsplit(".", 1)[1].lower()
    if extension == "csv": return clean_dataframe(pd.read_csv(path))
    if extension in {"xls", "xlsx"}: return clean_dataframe(pd.read_excel(path, engine="xlrd" if extension == "xls" else "openpyxl"))
    if extension != "pdf": raise ValueError("Unsupported data file type.")
    tables = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                rows = [row for row in table if row and any(cell is not None and str(cell).strip() for cell in row)]
                if len(rows) < 2: continue
                headers = [str(value).strip() if value else f"Column_{index + 1}" for index, value in enumerate(rows[0])]
                tables.append(pd.DataFrame([(list(row) + [None] * len(headers))[:len(headers)] for row in rows[1:]], columns=headers))
    if not tables: raise ValueError("No readable tables were found in the PDF.")
    return clean_dataframe(pd.concat(tables, ignore_index=True))


def extract_emails_from_dataframe(df):
    column = find_email_column(df.columns)
    if not column: raise ValueError(f"No Email column found. Detected columns: {', '.join(map(str, df.columns)) or 'None'}")
    emails, seen, invalid = [], set(), 0
    for value in df[column]:
        if pd.isna(value): continue
        email = str(value).strip().lower()
        if not email: continue
        if validate_email(email) and email not in seen: emails.append(email); seen.add(email)
        elif not validate_email(email): invalid += 1
    if not emails: raise ValueError("Email column found, but no valid email addresses were detected.")
    return column, emails, invalid


def parse_emails(text):
    seen, emails = set(), []
    for raw in re.split(r"[\n,;]+", text or ""):
        email = raw.strip().lower()
        if email and validate_email(email) and email not in seen: emails.append(email); seen.add(email)
    return emails


def save_attachments(campaign_id):
    """Stream each attachment once to its campaign directory; never store bytes in SQLite."""
    attachments, total = [], 0
    files = [upload for upload in request.files.getlist("attachments[]") + request.files.getlist("attachment") if upload and upload.filename]
    if not files:
        return attachments
    required = request.content_length or 0
    available = shutil.disk_usage(Config.CAMPAIGN_ATTACHMENT_FOLDER).free
    if available < required + 5 * 1024 * 1024:
        raise ValueError(f"Insufficient disk space to create this campaign (available: {available / 1024 / 1024:.1f} MB).")
    directory = os.path.join(Config.CAMPAIGN_ATTACHMENT_FOLDER, campaign_id)
    os.makedirs(directory, exist_ok=False)
    try:
        used_names = set()
        for upload in files:
            if not upload or not upload.filename: continue
            filename = secure_filename(upload.filename)
            if not filename or not allowed_attachment_file(filename): raise ValueError(f"Attachment '{upload.filename}' has an unsupported file type.")
            stem, extension = os.path.splitext(filename); candidate, index = filename, 2
            while candidate.lower() in used_names:
                candidate = f"{stem}_{index}{extension}"; index += 1
            used_names.add(candidate.lower())
            path, size = os.path.join(directory, candidate), 0
            with open(path, "wb") as target:
                while chunk := upload.stream.read(1024 * 1024):
                    size += len(chunk); total += len(chunk)
                    if size > Config.MAX_ATTACHMENT_SIZE: raise ValueError(f"Attachment '{candidate}' exceeds MAX_ATTACHMENT_SIZE.")
                    if total > Config.MAX_TOTAL_ATTACHMENT_SIZE: raise ValueError("Attachments exceed MAX_TOTAL_ATTACHMENT_SIZE.")
                    target.write(chunk)
            if not size: raise ValueError(f"Attachment '{candidate}' is empty.")
            attachments.append({"filename": candidate, "file_path": os.path.abspath(path), "size": size, "mime_type": upload.mimetype or None})
        return attachments
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def parse_schedule():
    scheduled_date, scheduled_time = request.form.get("schedule_date", ""), request.form.get("schedule_time", "")
    if not scheduled_date and not scheduled_time: return None, None
    if not scheduled_date or not scheduled_time: raise ValueError("Both schedule date and time are required.")
    timezone_name = request.form.get("timezone", Config.SCHEDULER_TIMEZONE)
    try:
        scheduled = datetime.fromisoformat(f"{scheduled_date}T{scheduled_time}").replace(tzinfo=ZoneInfo(timezone_name))
    except (ValueError, TypeError, KeyError): raise ValueError("Schedule date, time, or timezone is invalid.")
    if scheduled <= datetime.now(ZoneInfo(timezone_name)): raise ValueError("Scheduled time must be in the future.")
    return scheduled.astimezone(ZoneInfo("UTC")).isoformat(), timezone_name


@app.route("/")
def index(): return render_template("index.html")


@app.route("/upload-file", methods=["POST"])
def upload_file():
    try:
        upload = request.files.get("file")
        if not upload or not upload.filename: return jsonify(success=False, message="Please select a PDF, CSV, XLS, or XLSX file."), 400
        if not allowed_data_file(upload.filename): return jsonify(success=False, message="Unsupported file type. Please upload PDF, CSV, XLS, or XLSX."), 400
        filename = secure_filename(upload.filename)
        content = upload.read()
        if not content: return jsonify(success=False, message="The uploaded file is empty."), 400
        if len(content) > Config.MAX_CONTACT_FILE_SIZE: return jsonify(success=False, message="Contact file exceeds MAX_CONTACT_FILE_SIZE."), 400
        path = os.path.join(app.config["UPLOAD_FOLDER"], f"contact_{os.urandom(8).hex()}_{filename}")
        with open(path, "wb") as file: file.write(content)
        try:
            df = parse_data_file(path, filename); column, emails, invalid = extract_emails_from_dataframe(df)
        finally: os.remove(path)
        return jsonify(success=True, message="File uploaded and processed successfully.", filename=filename, email_column=str(column), total_records=int(len(df)), valid_email_count=len(emails), invalid_email_count=invalid, emails=emails, next_step="Email addresses extracted successfully. Enter the subject and message, optionally attach files, then send.")
    except ValueError as exc: return jsonify(success=False, message=str(exc)), 400
    except pd.errors.EmptyDataError: return jsonify(success=False, message="The uploaded file is empty."), 400
    except Exception as exc:
        app.logger.exception("Upload processing error"); return jsonify(success=False, message=f"Could not process the file: {exc}"), 500


@app.route("/send-emails", methods=["POST"])
def send_emails():
    campaign_id = None
    try:
        Config.validate_smtp_config()
        emails, subject, body = parse_emails(request.form.get("emails")), request.form.get("subject", "").strip(), request.form.get("body", "").strip()
        if not emails: return jsonify(success=False, message="No valid email addresses were found."), 400
        if not subject or not body: return jsonify(success=False, message="Subject and email body are required."), 400
        campaign_id = str(uuid.uuid4()); attachments = save_attachments(campaign_id); estimated_size = estimate_message_size(body, attachments)
        if estimated_size > Config.SMTP_MAX_MESSAGE_SIZE:
            raise ValueError(f"Estimated email size ({estimated_size / 1024 / 1024:.2f} MB) exceeds the configured SMTP limit ({Config.SMTP_MAX_MESSAGE_SIZE / 1024 / 1024:.2f} MB).")
        scheduled_at, timezone_name = parse_schedule()
        campaign_id = database.create_campaign(emails, subject, body, attachments, scheduled_at, timezone_name, request.form.get("campaign_name", "").strip() or None, campaign_id)
        if not scheduled_at: campaign_manager.enqueue(campaign_id)
        campaign = database.get_campaign(campaign_id)
        return jsonify(success=True, message="Campaign scheduled." if scheduled_at else "Campaign accepted and queued.", campaign_id=campaign_id, estimated_email_size=estimated_size, **campaign), 202
    except ValueError as exc:
        if campaign_id: shutil.rmtree(os.path.join(Config.CAMPAIGN_ATTACHMENT_FOLDER, campaign_id), ignore_errors=True)
        return jsonify(success=False, message=str(exc)), 400
    except sqlite3.OperationalError as exc:
        if campaign_id: shutil.rmtree(os.path.join(Config.CAMPAIGN_ATTACHMENT_FOLDER, campaign_id), ignore_errors=True)
        app.logger.exception("Campaign storage error")
        message = "Unable to create campaign because storage is full." if "full" in str(exc).lower() else "Unable to create campaign because the database could not be written."
        return jsonify(success=False, message=message), 507 if "full" in str(exc).lower() else 500
    except RuntimeError as exc: return jsonify(success=False, message=str(exc)), 503
    except Exception as exc:
        if campaign_id: shutil.rmtree(os.path.join(Config.CAMPAIGN_ATTACHMENT_FOLDER, campaign_id), ignore_errors=True)
        app.logger.exception("Campaign creation error"); return jsonify(success=False, message="Could not create campaign."), 500


@app.route("/campaign/<campaign_id>/status")
def campaign_status(campaign_id):
    campaign = database.get_campaign(campaign_id)
    return (jsonify(success=True, campaign=campaign, **campaign), 200) if campaign else (jsonify(success=False, message="Campaign not found."), 404)


@app.route("/campaigns")
def campaigns(): return jsonify(success=True, campaigns=database.list_campaigns())


@app.route("/campaign/<campaign_id>/cancel", methods=["POST"])
def cancel_campaign(campaign_id):
    campaign = database.cancel_campaign(campaign_id)
    return (jsonify(success=True, campaign=campaign, **campaign), 200) if campaign else (jsonify(success=False, message="Campaign not found."), 404)


@app.route("/campaign/<campaign_id>/retry-failed", methods=["POST"])
def retry_failed(campaign_id):
    new_id = database.retry_failed_campaign(campaign_id)
    if not new_id: return jsonify(success=False, message="No failed recipients are available to retry."), 400
    campaign_manager.enqueue(new_id); campaign = database.get_campaign(new_id)
    return jsonify(success=True, message="Failed recipients queued for retry.", campaign_id=new_id, **campaign), 202


@app.route("/campaign/<campaign_id>", methods=["DELETE"])
def delete_campaign(campaign_id):
    return (jsonify(success=True), 200) if database.delete_campaign(campaign_id) else (jsonify(success=False, message="Campaign not found."), 404)


@app.errorhandler(RequestEntityTooLarge)
def request_too_large(_): return jsonify(success=False, message="Upload exceeds MAX_CONTENT_LENGTH."), 413


if __name__ == "__main__": app.run(debug=False)
