
import os
import re
import smtplib
import pandas as pd
import pdfplumber
import time

from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from config import Config


app = Flask(__name__)
app.config.from_object(Config)

# Make sure upload folder exists.
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


EMAIL_REGEX = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")


def allowed_data_file(filename):
    """Check whether the uploaded contact file has a supported extension."""
    if not filename or "." not in filename:
        return False

    extension = filename.rsplit(".", 1)[1].lower()
    return extension in Config.ALLOWED_DATA_EXTENSIONS


def allowed_attachment_file(filename):
    """Check whether the attachment has a supported extension."""
    if not filename or "." not in filename:
        return False

    extension = filename.rsplit(".", 1)[1].lower()
    return extension in Config.ALLOWED_ATTACHMENT_EXTENSIONS


def validate_email(email):
    """Basic email format validation."""
    if not isinstance(email, str):
        return False

    email = email.strip().lower()

    if not EMAIL_REGEX.match(email):
        return False

    blocked_domains = {
        "example.com",
        "example.org",
        "example.net",
        "txt.com"
    }

    domain = email.split("@")[1]

    if domain in blocked_domains:
        return False

    return True


def normalize_column_name(column):
    """Normalize a column name for reliable Email-column detection."""
    return re.sub(r"[^a-z0-9]", "", str(column).strip().lower())


def find_email_column(columns):
    """
    Find an Email-related column.
    Supports examples like:
    Email, E-mail, Email Address, E-mail Address, email_id, emailId
    """
    exact_names = {
        "email",
        "emailaddress",
        "emailid",
        "mail",
        "Email"
    }

    normalized_columns = {normalize_column_name(col): col for col in columns}

    # Prefer exact known names.
    for normalized, original in normalized_columns.items():
        if normalized in exact_names:
            return original

    # Fallback for names such as customeremail / primaryemail.
    for normalized, original in normalized_columns.items():
        if "email" in normalized or normalized == "mail":
            return original

    return None


def clean_dataframe(df):
    """Clean headers and remove fully empty rows."""
    df = df.copy()
    df.columns = [str(col).strip() for col in df.columns]
    df = df.dropna(how="all")
    return df


def parse_csv(file_path):
    """Read CSV into a DataFrame."""
    return clean_dataframe(pd.read_csv(file_path))


def parse_excel(file_path, extension):
    """Read XLS/XLSX into a DataFrame."""
    engine = "xlrd" if extension == "xls" else "openpyxl"
    return clean_dataframe(pd.read_excel(file_path, engine=engine))


def parse_pdf(file_path):
    """
    Extract table data from a PDF using pdfplumber.

    This basic implementation expects the PDF to contain a table with
    a header row containing an Email-related column.
    """
    all_tables = []

    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()

            for table in tables:
                if not table:
                    continue

                # Remove completely empty rows.
                table = [
                    row for row in table
                    if row and any(cell is not None and str(cell).strip() for cell in row)
                ]

                if not table:
                    continue

                header = table[0]
                data = table[1:]

                if not header:
                    continue

                headers = []
                for index, value in enumerate(header):
                    text = "" if value is None else str(value).strip()
                    headers.append(text if text else f"Column_{index + 1}")

                # Make row lengths match header length.
                normalized_rows = []
                for row in data:
                    row = list(row)
                    row += [None] * (len(headers) - len(row))
                    normalized_rows.append(row[:len(headers)])

                if normalized_rows:
                    all_tables.append(pd.DataFrame(normalized_rows, columns=headers))

    if not all_tables:
        raise ValueError(
            "No readable tables were found in the PDF."
        )

    return clean_dataframe(pd.concat(all_tables, ignore_index=True))


def parse_data_file(file_path, filename):
    """Select the correct parser based on file extension."""
    extension = filename.rsplit(".", 1)[1].lower()

    if extension == "csv":
        return parse_csv(file_path)

    if extension in {"xls", "xlsx"}:
        return parse_excel(file_path, extension)

    if extension == "pdf":
        return parse_pdf(file_path)

    raise ValueError("Unsupported data file type.")


def extract_emails_from_dataframe(df):
    """
    Find the Email column, validate email addresses, remove duplicates,
    and preserve original order.
    """
    email_column = find_email_column(df.columns)

    if not email_column:
        detected_columns = ", ".join(str(col) for col in df.columns)
        raise ValueError(
            f"No Email column found in the uploaded file. "
            f"Detected columns: {detected_columns or 'None'}"
        )

    valid_emails = []
    invalid_count = 0
    seen = set()

    for value in df[email_column]:
        if pd.isna(value):
            continue

        email = str(value).strip().lower()

        if not email:
            continue

        if validate_email(email):
            if email not in seen:
                seen.add(email)
                valid_emails.append(email)
        else:
            invalid_count += 1

    if not valid_emails:
        raise ValueError(
            "Email column found, but no valid email addresses were detected."
        )

    return email_column, valid_emails, invalid_count


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload-file", methods=["POST"])
def upload_file():
    try:
        if "file" not in request.files:
            return jsonify({
                "success": False,
                "message": "Please select a PDF, CSV, XLS, or XLSX file."
            }), 400

        uploaded_file = request.files["file"]

        if not uploaded_file.filename:
            return jsonify({
                "success": False,
                "message": "No file selected."
            }), 400

        if not allowed_data_file(uploaded_file.filename):
            return jsonify({
                "success": False,
                "message": "Unsupported file type. Please upload PDF, CSV, XLS, or XLSX."
            }), 400

        filename = secure_filename(uploaded_file.filename)

        if not filename:
            return jsonify({
                "success": False,
                "message": "Invalid filename."
            }), 400

        file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        uploaded_file.save(file_path)

        try:
            df = parse_data_file(file_path, filename)
            email_column, emails, invalid_count = extract_emails_from_dataframe(df)

            return jsonify({
                "success": True,
                "message": "File uploaded and processed successfully.",
                "filename": filename,
                "email_column": str(email_column),
                "total_records": int(len(df)),
                "valid_email_count": int(len(emails)),
                "invalid_email_count": int(invalid_count),
                "emails": emails,
                "next_step": (
                    "Email addresses extracted successfully. "
                    "Enter the subject and message, optionally attach a file, then send."
                )
            })

        finally:
            # The contact file is no longer required after extraction.
            try:
                os.remove(file_path)
            except OSError:
                pass

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except pd.errors.EmptyDataError:
        return jsonify({
            "success": False,
            "message": "The uploaded file is empty."
        }), 400

    except Exception as exc:
        app.logger.exception("Upload processing error")
        return jsonify({
            "success": False,
            "message": f"Could not process the file: {exc}"
        }), 500


def build_message(sender, recipient, subject, body, attachment):
    """Create a MIME email with an optional attachment."""
    message = MIMEMultipart()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject

    message.attach(MIMEText(body, "plain", "utf-8"))

    if attachment:
        filename = secure_filename(attachment.filename)

        if not allowed_attachment_file(filename):
            raise ValueError("Attachment file type is not allowed.")

        attachment_bytes = attachment.read()

        if len(attachment_bytes) > Config.MAX_ATTACHMENT_SIZE:
            raise ValueError("Attachment is larger than the allowed size.")

        if not attachment_bytes:
            raise ValueError("The selected attachment is empty.")

        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment_bytes)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{filename}"'
        )
        message.attach(part)

    return message


@app.route("/send-emails", methods=["POST"])
def send_emails():
    smtp = None

    try:
        Config.validate_smtp_config()

        emails_text = request.form.get("emails", "").strip()
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()
        attachment = request.files.get("attachment")

        if not emails_text:
            return jsonify({
                "success": False,
                "message": "No email addresses were provided."
            }), 400

        if not subject:
            return jsonify({
                "success": False,
                "message": "Subject is required."
            }), 400

        if not body:
            return jsonify({
                "success": False,
                "message": "Email body is required."
            }), 400

        emails = []
        seen = set()

        # Accept newline, comma, or semicolon separated emails.
        raw_emails = re.split(r"[\n,;]+", emails_text)

        for raw_email in raw_emails:
            email = raw_email.strip().lower()

            if email and validate_email(email) and email not in seen:
                seen.add(email)
                emails.append(email)

        if not emails:
            return jsonify({
                "success": False,
                "message": "No valid email addresses were found."
            }), 400

        # Read attachment once so it can be recreated for every message.
        attachment_bytes = None
        attachment_filename = None

        if attachment and attachment.filename:
            attachment_filename = secure_filename(attachment.filename)

            if not allowed_attachment_file(attachment_filename):
                return jsonify({
                    "success": False,
                    "message": "Attachment file type is not allowed."
                }), 400

            attachment_bytes = attachment.read()

            if len(attachment_bytes) > Config.MAX_ATTACHMENT_SIZE:
                return jsonify({
                    "success": False,
                    "message": "Attachment is larger than the allowed size."
                }), 400

            if not attachment_bytes:
                return jsonify({
                    "success": False,
                    "message": "The selected attachment is empty."
                }), 400
                
        start_time = time.time()
        smtp = smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT, timeout=30)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(Config.SMTP_USERNAME, Config.SMTP_PASSWORD)

        sent = 0
        failed = []

        for recipient in emails:
            try:
                message = MIMEMultipart()
                message["From"] = Config.SMTP_USERNAME
                message["To"] = recipient
                message["Subject"] = subject
                message.attach(MIMEText(body, "plain", "utf-8"))

                if attachment_bytes is not None:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(attachment_bytes)
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f'attachment; filename="{attachment_filename}"'
                    )
                    message.attach(part)

                smtp.sendmail(
                    Config.SMTP_USERNAME,
                    recipient,
                    message.as_string()
                )

                sent += 1

            except Exception as exc:
                failed.append({
                    "email": recipient,
                    "error": str(exc)
                })
                
        end_time = time.time()
        total_time = round(end_time - start_time, 2)

        avg_time = round(total_time / sent, 2) if sent > 0 else 0
        
        return jsonify({
            "success": True,
            "message": "Campaign completed.",
            "total": len(emails),
            "sent": sent,
            "failed": len(failed),
            "failed_details": failed,
            "attachment_name": attachment_filename,
            "total_time": total_time,
            "avg_time_per_email": avg_time
        })

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except smtplib.SMTPAuthenticationError as exc:
        app.logger.exception("Zoho SMTP authentication failed")

        return jsonify({
            "success": False,
            "message": f"Zoho SMTP authentication failed: {exc}"
        }), 500

    except (smtplib.SMTPException, OSError) as exc:
        return jsonify({
            "success": False,
            "message": f"SMTP connection error: {exc}"
        }), 500

    except Exception as exc:
        app.logger.exception("Email sending error")
        return jsonify({
            "success": False,
            "message": f"Unexpected sending error: {exc}"
        }), 500

    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass


if __name__ == "__main__":
    app.run(debug=True)
   
