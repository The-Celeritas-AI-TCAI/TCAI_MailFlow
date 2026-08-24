"""Background campaign workers, MIME construction, and SMTP lifecycle management."""
import html
import logging
import mimetypes
import os
import queue
import smtplib
import socket
import threading
import time
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr

from config import Config
import database

logger = logging.getLogger(__name__)


class SMTPConfigurationError(ValueError):
    """A non-retryable error shared by every recipient in a campaign."""


def estimate_message_size(body, attachments, logo_size=0):
    """Conservative estimate including base64 expansion and MIME headers."""
    encoded_files = sum(((item["size"] + 2) // 3) * 4 + 512 for item in attachments)
    encoded_logo = ((logo_size + 2) // 3) * 4 + 512 if logo_size else 0
    return len(body.encode("utf-8")) * 2 + encoded_files + encoded_logo + 4096


class SMTPConnectionManager:
    """Persistent worker-local SMTP connection; never shared between threads."""
    def __init__(self):
        self.smtp = None
        self.stage = "CONNECT"

    def connect(self):
        self.discard()
        sender = Config.smtp_username()
        logger.info("[SMTP] server=%s port=%s username=%r", Config.SMTP_SERVER, Config.SMTP_PORT, sender)
        started = time.monotonic()
        smtp = None
        try:
            self.stage = "CONNECT"
            smtp = smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT, timeout=Config.SMTP_CONNECT_TIMEOUT)
            connected = time.monotonic()
            self.stage = "EHLO"
            smtp.ehlo()
            if Config.SMTP_USE_TLS:
                self.stage = "STARTTLS"
                smtp.starttls()
                self.stage = "EHLO"
                smtp.ehlo()
            tls_ready = time.monotonic()
            self.stage = "LOGIN"
            smtp.login(sender, Config.SMTP_PASSWORD)
            if smtp.sock:
                smtp.sock.settimeout(Config.SMTP_TIMEOUT)
            self.smtp = smtp
            logger.info("[PERF] SMTP connect=%.2fs TLS/EHLO=%.2fs login=%.2fs", connected - started, tls_ready - connected, time.monotonic() - tls_ready)
            return smtp
        except Exception:
            if smtp:
                try:
                    smtp.close()
                except OSError:
                    pass
            raise

    def is_connection_alive(self):
        if not self.smtp:
            return False
        try:
            status, _ = self.smtp.noop()
            return 200 <= status < 300
        except (smtplib.SMTPException, OSError, socket.timeout):
            self.disconnect()
            return False

    def _set_socket_timeout(self, timeout):
        if self.smtp and self.smtp.sock:
            self.smtp.sock.settimeout(timeout)

    def send_message(self, message_bytes, recipient, has_attachments=False):
        # A dead socket is discarded by the retry controller. Do not issue NOOP
        # for every recipient: it adds a round trip and can itself block.
        if not self.smtp:
            self.connect()
        data_timeout = Config.SMTP_ATTACHMENT_TIMEOUT if has_attachments else Config.SMTP_TIMEOUT
        self._set_socket_timeout(data_timeout)
        mail_options = []
        # SIZE lets servers with a lower policy limit reject at MAIL FROM,
        # before an attachment is uploaded during DATA.
        if self.smtp.has_extn("size"):
            mail_options.append(f"SIZE={len(message_bytes)}")
        sender = Config.smtp_username()
        try:
            # Use the explicit SMTP sequence so diagnostics always name the
            # command that failed.  sendmail() obscures this as a generic call.
            self.stage = "MAIL_FROM"
            code, response = self.smtp.mail(sender, options=mail_options)
            if not 200 <= code < 300:
                raise smtplib.SMTPSenderRefused(code, response, sender)
            self.stage = "RCPT_TO"
            code, response = self.smtp.rcpt(recipient)
            if not 200 <= code < 300:
                raise smtplib.SMTPRecipientsRefused({recipient: (code, response)})
            self.stage = "DATA"
            logger.info("[SMTP] recipient=%s DATA start bytes=%s timeout=%ss", recipient, len(message_bytes), data_timeout)
            code, response = self.smtp.data(message_bytes)
            if not 200 <= code < 300:
                raise smtplib.SMTPDataError(code, response)
        finally:
            # A healthy persistent connection returns to the fast text timeout.
            self._set_socket_timeout(Config.SMTP_TIMEOUT)

    def discard(self):
        """Drop a failed connection without SMTP QUIT, which can block on a dead peer."""
        if self.smtp:
            try:
                self.smtp.close()
            except OSError:
                pass
            finally:
                self.smtp = None

    def disconnect(self):
        """Gracefully close a known-good connection when a worker is shut down."""
        if self.smtp:
            try:
                self.smtp.quit()
            except (smtplib.SMTPException, OSError, socket.timeout):
                self.discard()
            else:
                self.smtp = None


class EmailMessageBuilder:
    """Builds an independent multipart message for each recipient."""
    def __init__(self):
        self._logo = self._load_logo()

    @staticmethod
    def _load_logo():
        path = Config.COMPANY_LOGO_PATH
        if not path:
            return None
        if not os.path.isfile(path):
            logger.warning("Configured COMPANY_LOGO_PATH does not exist; sending signature without logo.")
            return None
        with open(path, "rb") as logo_file:
            content = logo_file.read()
        mime_type = mimetypes.guess_type(path)[0] or "image/png"
        maintype, subtype = mime_type.split("/", 1)
        if not content or maintype != "image":
            logger.warning("Configured company logo is invalid; sending signature without logo.")
            return None
        return {"content": content, "maintype": maintype, "subtype": subtype, "filename": os.path.basename(path)}

    @property
    def logo_size(self):
        return len(self._logo["content"]) if self._logo else 0

    def _signature_html(self):
        website = Config.SIGNATURE_WEBSITE.strip()
        href = website if website.startswith(("http://", "https://")) else f"https://{website}"
        logo = (f'<br><img src="cid:{Config.COMPANY_LOGO_CID}" alt="Company logo" '
                'style="display:block;margin-top:12px;max-width:160px;height:auto;border:0;">') if self._logo else ""
        return ("<br><br>Warm regards,<br><br>"
                f"{html.escape(Config.SIGNATURE_NAME)}<br>{html.escape(Config.SIGNATURE_TEAM)}<br>"
                f"{html.escape(Config.SIGNATURE_PHONE)}<br>"
                f'<a href="{html.escape(href, quote=True)}">{html.escape(website)}</a>{logo}')

    def build(self, sender, recipient, subject, body, attachments):
        if any("\r" in value or "\n" in value for value in (recipient, subject)):
            raise ValueError("Recipient and subject must not contain line breaks.")
        message = EmailMessage(policy=SMTP)
        # Display formatting belongs only in the MIME header, never MAIL FROM.
        message["From"], message["To"], message["Subject"] = formataddr(("MailFlow", sender)), recipient, subject
        message.set_content(body + "\n\nWarm regards,\n\n" + Config.SIGNATURE_NAME)
        body_html = html.escape(body).replace("\n", "<br>\n")
        message.add_alternative(f"<html><body>{body_html}{self._signature_html()}</body></html>", subtype="html")
        if self._logo:
            html_part = message.get_payload()[-1]
            html_part.add_related(self._logo["content"], maintype=self._logo["maintype"], subtype=self._logo["subtype"],
                                  cid=f"<{Config.COMPANY_LOGO_CID}>", disposition="inline", filename=self._logo["filename"])
        for attachment in attachments:
            mime_type = mimetypes.guess_type(attachment["filename"])[0] or "application/octet-stream"
            maintype, subtype = mime_type.split("/", 1)
            message.add_attachment(attachment["content"], maintype=maintype, subtype=subtype, filename=attachment["filename"])
        return message


class CampaignManager:
    def __init__(self):
        self.campaign_queue = queue.Queue(maxsize=100)
        self.recipient_queue = queue.Queue(maxsize=Config.EMAIL_QUEUE_SIZE)
        self.enqueued, self.lock = set(), threading.Lock()
        self.rate_lock, self.last_send, self.started = threading.Lock(), 0, False
        self.payload_cache, self.payload_lock = {}, threading.Lock()

    def start(self):
        if self.started:
            return
        self.started = True
        for index in range(Config.EMAIL_WORKERS):
            threading.Thread(target=self._worker, name=f"mailflow-smtp-{index + 1}", daemon=True).start()
        threading.Thread(target=self._dispatcher, name="mailflow-dispatcher", daemon=True).start()
        threading.Thread(target=self._scheduler, name="mailflow-scheduler", daemon=True).start()
        for campaign_id in database.recover_campaigns():
            self.enqueue(campaign_id)

    def enqueue(self, campaign_id):
        with self.lock:
            if campaign_id in self.enqueued:
                return
            self.enqueued.add(campaign_id)
        try:
            self.campaign_queue.put_nowait(campaign_id)
        except queue.Full:
            with self.lock:
                self.enqueued.discard(campaign_id)
            raise RuntimeError("Campaign queue is busy; please try again shortly.")

    def _dispatcher(self):
        while True:
            campaign_id = self.campaign_queue.get()
            try:
                for recipient_id in database.queued_recipient_ids(campaign_id):
                    self.recipient_queue.put(recipient_id)
            finally:
                with self.lock:
                    self.enqueued.discard(campaign_id)
                self.campaign_queue.task_done()

    def _scheduler(self):
        while True:
            try:
                for campaign_id in database.activate_due_campaigns():
                    self.enqueue(campaign_id)
                database.cleanup_expired_attachment_files(Config.ATTACHMENT_RETENTION_SECONDS)
            except Exception:
                logger.exception("Scheduled campaign check failed")
            time.sleep(5)

    def _payload(self, campaign_id):
        with self.payload_lock:
            if campaign_id not in self.payload_cache:
                campaign, attachments = database.campaign_payload(campaign_id)
                prepared = []
                for attachment in attachments:
                    if "content" in attachment:
                        prepared.append(attachment)
                        continue
                    try:
                        with open(attachment["file_path"], "rb") as source:
                            prepared.append({**attachment, "content": source.read()})
                    except OSError as exc:
                        raise RuntimeError(f"Attachment '{attachment['filename']}' cannot be read: {exc}") from exc
                self.payload_cache[campaign_id] = campaign, prepared
            return self.payload_cache[campaign_id]

    def _release_payload_if_finished(self, campaign_id):
        campaign = database.get_campaign(campaign_id, include_failed=False)
        if campaign and campaign["status"] in database.TERMINAL_CAMPAIGN_STATUSES:
            with self.payload_lock:
                self.payload_cache.pop(campaign_id, None)

    def _wait_for_rate_limit(self):
        if not Config.EMAIL_RATE_LIMIT:
            return
        with self.rate_lock:
            wait = self.last_send + 1 / Config.EMAIL_RATE_LIMIT - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_send = time.monotonic()

    @staticmethod
    def _error_details(exc):
        if isinstance(exc, smtplib.SMTPAuthenticationError): return "SMTP authentication failed", False, True
        if isinstance(exc, smtplib.SMTPRecipientsRefused): return "Recipient rejected", False, False
        if isinstance(exc, smtplib.SMTPSenderRefused): return "SMTP sender address rejected. Verify the authenticated Zoho sender address.", False, True
        if isinstance(exc, smtplib.SMTPDataError):
            return ("Message exceeds SMTP size limit", False, False) if exc.smtp_code in (552, 554) else (f"SMTP data rejected ({exc.smtp_code})", exc.smtp_code in (421, 450, 451, 452), False)
        if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, socket.timeout, TimeoutError, ConnectionError, OSError)):
            return "SMTP connection lost or timed out", True, False
        if isinstance(exc, smtplib.SMTPResponseException): return f"SMTP server rejected message ({exc.smtp_code})", exc.smtp_code in (421, 450, 451, 452), False
        return "SMTP server error", True, False

    def _worker(self):
        connection, builder = SMTPConnectionManager(), EmailMessageBuilder()
        while True:
            recipient_id = self.recipient_queue.get()
            claimed = None
            try:
                claimed = database.claim_recipient(recipient_id)
                if not claimed:
                    continue
                campaign, attachments = self._payload(claimed["campaign_id"])
                if estimate_message_size(campaign["body"], attachments, builder.logo_size) > Config.SMTP_MAX_MESSAGE_SIZE:
                    database.recipient_result(recipient_id, "failed", "Message exceeds configured SMTP size limit")
                    continue
                build_started = time.monotonic()
                sender = Config.smtp_username()
                message = builder.build(sender, claimed["email"], campaign["subject"], campaign["body"], attachments)
                message_bytes = message.as_bytes()
                build_elapsed = time.monotonic() - build_started
                if len(message_bytes) > Config.SMTP_MAX_MESSAGE_SIZE:
                    database.recipient_result(recipient_id, "failed", "Generated MIME message exceeds configured SMTP size limit")
                    continue
                logger.info("[PERF] recipient=%s build_and_encode=%.2fs size=%.2fMB", claimed["email"], build_elapsed, len(message_bytes) / 1024 / 1024)
                for attempt in range(1, Config.MAX_EMAIL_RETRIES + 2):
                    database.record_attempt(recipient_id)
                    send_started = time.monotonic()
                    try:
                        self._wait_for_rate_limit(); connection.send_message(message_bytes, claimed["email"], bool(attachments))
                        database.recipient_result(recipient_id, "sent")
                        logger.info("[PERF] recipient=%s attempt=%s send=%.2fs total=%.2fs SUCCESS", claimed["email"], attempt, time.monotonic() - send_started, time.monotonic() - build_started)
                        break
                    except (smtplib.SMTPException, OSError, socket.timeout, TimeoutError, ConnectionError) as exc:
                        reason, retryable, fatal_auth = self._error_details(exc)
                        logger.warning("recipient=%s attempt=%s stage=%s elapsed=%.2fs error=%s detail=%s reason=%s", claimed["email"], attempt, connection.stage, time.monotonic() - send_started, type(exc).__name__, str(exc), reason)
                        connection.discard()
                        if fatal_auth:
                            database.fail_campaign_configuration(claimed["campaign_id"], reason)
                            break
                        if not retryable or attempt > Config.MAX_EMAIL_RETRIES:
                            database.recipient_result(recipient_id, "failed", reason)
                            break
                        logger.info("recipient=%s retrying once after %.2fs", claimed["email"], Config.RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                        time.sleep(Config.RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                    except Exception:
                        logger.exception("%s | unexpected send failure", claimed["email"])
                        database.recipient_result(recipient_id, "failed", "Unexpected application error")
                        break
            except (SMTPConfigurationError, ValueError) as exc:
                logger.error("SMTP configuration error: %s", exc)
                if claimed:
                    database.fail_campaign_configuration(claimed["campaign_id"], f"SMTP sender configuration error: {exc}")
            except Exception as exc:
                # Payload/file failures occur before the send retry loop. They
                # still belong to this recipient and must never strand it in
                # 'sending' or stop later recipients.
                logger.exception("recipient=%s could not be prepared", claimed["email"] if claimed else recipient_id)
                if claimed:
                    database.recipient_result(recipient_id, "failed", f"Attachment or campaign preparation failed: {exc}")
            finally:
                if claimed:
                    self._release_payload_if_finished(claimed["campaign_id"])
                self.recipient_queue.task_done()


campaign_manager = CampaignManager()
