"""Background campaign workers, MIME construction, and SMTP lifecycle management."""

import html
import logging
import mimetypes
import os
import queue
import select
import smtplib
import socket
import ssl
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


def _redacted_recipient(address):
    """Retain enough context for SMTP diagnostics without logging full addresses."""
    local, separator, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if separator else "***"


def estimate_message_size(body, attachments, logo_size=0):
    """Conservative estimate including base64 expansion and MIME headers."""
    encoded_files = sum(
        ((item["size"] + 2) // 3) * 4 + 512
        for item in attachments
    )

    encoded_logo = (
        ((logo_size + 2) // 3) * 4 + 512
        if logo_size
        else 0
    )

    return (
        len(body.encode("utf-8")) * 2
        + encoded_files
        + encoded_logo
        + 4096
    )


class ResilientSMTP(smtplib.SMTP):
    """
    SMTP client that handles TLS back-pressure safely.

    The important behavior here is that the DATA timeout is an IDLE timeout.

    If bytes continue to make progress, the deadline is renewed.

    Therefore:
        slow but progressing transfer -> continue
        genuinely stalled transfer   -> timeout
    """

    TLS_WRITE_CHUNK_SIZE = 64 * 1024

    def send(self, data):
        if not self.sock:
            raise smtplib.SMTPServerDisconnected(
                "Server not connected"
            )

        if isinstance(data, str):
            data = data.encode("ascii")

        view = memoryview(data)

        timeout = self.sock.gettimeout()

        deadline = (
            None
            if timeout is None
            else time.monotonic() + timeout
        )

        try:
            while view:
                try:
                    # Never hand one huge DATA buffer directly to SSLSocket.
                    # Chunking prevents Windows/OpenSSL write stalls from
                    # appearing as immediate SMTP disconnects.
                    written = self.sock.send(
                        view[: self.TLS_WRITE_CHUNK_SIZE]
                    )

                    if not written:
                        raise smtplib.SMTPServerDisconnected(
                            "SMTP connection closed while sending"
                        )

                    view = view[written:]

                    # IMPORTANT:
                    # Successful progress renews the idle timeout.
                    #
                    # This means a 100 MB message can take longer than the
                    # configured timeout overall as long as the socket keeps
                    # making progress.
                    deadline = (
                        None
                        if timeout is None
                        else time.monotonic() + timeout
                    )

                except ssl.SSLWantWriteError:
                    self._wait_for_tls_io(
                        needs_read=False,
                        deadline=deadline,
                    )

                except ssl.SSLWantReadError:
                    self._wait_for_tls_io(
                        needs_read=True,
                        deadline=deadline,
                    )

        except (OSError, ValueError, socket.timeout) as exc:
            self.close()

            raise smtplib.SMTPServerDisconnected(
                f"SMTP connection lost during send: {exc}"
            ) from exc

    def _wait_for_tls_io(self, needs_read, deadline):
        remaining = (
            None
            if deadline is None
            else deadline - time.monotonic()
        )

        if remaining is not None and remaining <= 0:
            raise socket.timeout(
                "SMTP TLS write timed out due to inactivity"
            )

        readable, writable, _ = select.select(
            [self.sock] if needs_read else [],
            [] if needs_read else [self.sock],
            [],
            remaining,
        )

        if not readable and not writable:
            raise socket.timeout(
                "SMTP TLS write timed out due to inactivity"
            )


class SMTPConnectionManager:
    """
    Persistent worker-local SMTP connection.

    Each worker owns its own SMTP session.
    Connections are never shared between threads.
    """

    def __init__(self):
        self.smtp = None
        self.stage = "CONNECT"

    def connect(self):
        self.discard()

        sender = Config.smtp_username()

        logger.info(
            "[SMTP] connecting to configured server=%s port=%s",
            Config.SMTP_SERVER,
            Config.SMTP_PORT,
        )

        started = time.monotonic()

        smtp = None

        try:
            # ----------------------------------------------------------
            # CONNECT
            # ----------------------------------------------------------
            self.stage = "CONNECT"

            smtp = ResilientSMTP(
                Config.SMTP_SERVER,
                Config.SMTP_PORT,
                timeout=Config.SMTP_CONNECT_TIMEOUT,
            )

            connected = time.monotonic()

            # ----------------------------------------------------------
            # EHLO
            # ----------------------------------------------------------
            self.stage = "EHLO"
            smtp.ehlo()

            # ----------------------------------------------------------
            # STARTTLS
            # ----------------------------------------------------------
            if Config.SMTP_USE_TLS:
                self.stage = "STARTTLS"
                smtp.starttls()

                self.stage = "EHLO"
                smtp.ehlo()

            tls_ready = time.monotonic()

            # ----------------------------------------------------------
            # LOGIN
            # ----------------------------------------------------------
            self.stage = "LOGIN"

            smtp.login(
                sender,
                Config.SMTP_PASSWORD,
            )

            # After connection/authentication, use normal command timeout.
            if smtp.sock:
                smtp.sock.settimeout(
                    Config.SMTP_TIMEOUT
                )

            self.smtp = smtp

            logger.info(
                "[PERF] SMTP connect=%.2fs TLS/EHLO=%.2fs login=%.2fs",
                connected - started,
                tls_ready - connected,
                time.monotonic() - tls_ready,
            )

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

        except (
            smtplib.SMTPException,
            OSError,
            socket.timeout,
        ):
            self.disconnect()
            return False

    def _set_socket_timeout(self, timeout):
        if self.smtp and self.smtp.sock:
            self.smtp.sock.settimeout(timeout)

    def send_message(
        self,
        message_bytes,
        recipient,
        has_attachments=False,
    ):
        """
        Send one message over the persistent worker-local SMTP session.

        DATA uses a longer idle timeout.

        There is deliberately NO external watchdog that blindly destroys
        the socket after N seconds. ResilientSMTP itself tracks whether
        the socket is making progress.
        """

        if not self.smtp:
            self.connect()

        data_timeout = (
            Config.SMTP_ATTACHMENT_TIMEOUT
            if has_attachments
            else Config.SMTP_TIMEOUT
        )

        self._set_socket_timeout(data_timeout)

        smtp = self.smtp

        mail_options = []

        # Let the SMTP server enforce SIZE policy at MAIL FROM where
        # supported, before uploading the DATA body.
        if smtp.has_extn("size"):
            mail_options.append(
                f"SIZE={len(message_bytes)}"
            )

        sender = Config.smtp_username()

        try:
            # ----------------------------------------------------------
            # MAIL FROM
            # ----------------------------------------------------------
            self.stage = "MAIL_FROM"

            code, response = smtp.mail(
                sender,
                options=mail_options,
            )

            if not 200 <= code < 300:
                raise smtplib.SMTPSenderRefused(
                    code,
                    response,
                    sender,
                )

            # ----------------------------------------------------------
            # RCPT TO
            # ----------------------------------------------------------
            self.stage = "RCPT_TO"

            code, response = smtp.rcpt(
                recipient
            )

            if not 200 <= code < 300:
                raise smtplib.SMTPRecipientsRefused(
                    {
                        recipient: (
                            code,
                            response,
                        )
                    }
                )

            # ----------------------------------------------------------
            # DATA
            # ----------------------------------------------------------
            self.stage = "DATA"

            logger.info(
                "[SMTP] DATA start recipient=%s bytes=%s "
                "timeout=%ss",
                _redacted_recipient(recipient),
                len(message_bytes),
                data_timeout,
            )

            data_started = time.monotonic()

            code, response = smtp.data(
                message_bytes
            )

            data_elapsed = (
                time.monotonic() - data_started
            )

            logger.info(
                "[SMTP] DATA complete recipient=%s "
                "elapsed=%.2fs code=%s",
                _redacted_recipient(recipient),
                data_elapsed,
                code,
            )

            if not 200 <= code < 300:
                raise smtplib.SMTPDataError(
                    code,
                    response,
                )

        finally:
            # Always restore the normal command timeout.
            self._set_socket_timeout(
                Config.SMTP_TIMEOUT
            )

    def discard(self):
        """
        Drop a failed connection without SMTP QUIT.

        QUIT can itself block when the peer is already dead.
        """

        if self.smtp:
            try:
                self.smtp.close()
            except OSError:
                pass
            finally:
                self.smtp = None

        self.stage = "DISCONNECTED"

    def disconnect(self):
        """
        Gracefully close a known-good SMTP connection.
        """

        if self.smtp:
            try:
                self.smtp.quit()

            except (
                smtplib.SMTPException,
                OSError,
                socket.timeout,
            ):
                self.discard()

            else:
                self.smtp = None
                self.stage = "DISCONNECTED"


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
            logger.warning(
                "Configured COMPANY_LOGO_PATH does not exist; "
                "sending signature without logo."
            )
            return None

        with open(path, "rb") as logo_file:
            content = logo_file.read()

        mime_type = (
            mimetypes.guess_type(path)[0]
            or "image/png"
        )

        maintype, subtype = mime_type.split(
            "/",
            1,
        )

        if not content or maintype != "image":
            logger.warning(
                "Configured company logo is invalid; "
                "sending signature without logo."
            )
            return None

        return {
            "content": content,
            "maintype": maintype,
            "subtype": subtype,
            "filename": os.path.basename(path),
        }

    @property
    def logo_size(self):
        return (
            len(self._logo["content"])
            if self._logo
            else 0
        )

    def _signature_html(self):
        website = Config.SIGNATURE_WEBSITE.strip()

        href = (
            website
            if website.startswith(
                ("http://", "https://")
            )
            else f"https://{website}"
        )

        logo = (
            f'<br><img src="cid:{Config.COMPANY_LOGO_CID}" '
            'alt="Company logo" '
            'style="display:block;margin-top:12px;'
            'max-width:160px;height:auto;'
            'border:0;">'
            if self._logo
            else ""
        )

        return (
            "<br><br>Warm regards,<br><br>"
            f"{html.escape(Config.SIGNATURE_NAME)}<br>"
            f"{html.escape(Config.SIGNATURE_TEAM)}<br>"
            f"{html.escape(Config.SIGNATURE_PHONE)}<br>"
            f'<a href="{html.escape(href, quote=True)}">'
            f"{html.escape(website)}</a>"
            f"{logo}"
        )

    def build(
        self,
        sender,
        recipient,
        subject,
        body,
        attachments,
    ):
        if any(
            "\r" in value or "\n" in value
            for value in (
                recipient,
                subject,
            )
        ):
            raise ValueError(
                "Recipient and subject must not contain line breaks."
            )

        message = EmailMessage(
            policy=SMTP
        )

        # Display formatting belongs only in the MIME header.
        message["From"] = formataddr(
            ("TCAI", sender)
        )

        message["To"] = recipient
        message["Subject"] = subject

        message.set_content(
            body
            + "\n\nWarm regards,\n\n"
            + Config.SIGNATURE_NAME
        )

        body_html = (
            html.escape(body)
            .replace("\n", "<br>\n")
        )

        message.add_alternative(
            f"<html><body>"
            f"{body_html}"
            f"{self._signature_html()}"
            f"</body></html>",
            subtype="html",
        )

        if self._logo:
            html_part = (
                message.get_payload()[-1]
            )

            html_part.add_related(
                self._logo["content"],
                maintype=self._logo["maintype"],
                subtype=self._logo["subtype"],
                cid=f"<{Config.COMPANY_LOGO_CID}>",
                disposition="inline",
                filename=self._logo["filename"],
            )

        for attachment in attachments:
            mime_type = (
                mimetypes.guess_type(
                    attachment["filename"]
                )[0]
                or "application/octet-stream"
            )

            maintype, subtype = mime_type.split(
                "/",
                1,
            )

            message.add_attachment(
                attachment["content"],
                maintype=maintype,
                subtype=subtype,
                filename=attachment["filename"],
            )

        return message


class CampaignManager:
    def __init__(self):
        self.campaign_queue = queue.Queue(
            maxsize=100
        )

        self.recipient_queue = queue.Queue(
            maxsize=Config.EMAIL_QUEUE_SIZE
        )

        self.enqueued = set()
        self.lock = threading.Lock()

        self.rate_lock = threading.Lock()
        self.last_send = 0

        self.started = False

        self.payload_cache = {}
        self.payload_lock = threading.Lock()

    def start(self):
        if self.started:
            return

        self.started = True

        recovered_campaigns = (
            database.recover_campaigns()
        )

        if recovered_campaigns:
            logger.info(
                "[STARTUP] recovered %s interrupted "
                "campaign(s) as paused; none were "
                "automatically started",
                len(recovered_campaigns),
            )
        else:
            logger.info(
                "[STARTUP] no interrupted campaigns recovered"
            )

        for index in range(
            Config.EMAIL_WORKERS
        ):
            threading.Thread(
                target=self._worker,
                name=f"mailflow-smtp-{index + 1}",
                daemon=True,
            ).start()

        threading.Thread(
            target=self._dispatcher,
            name="mailflow-dispatcher",
            daemon=True,
        ).start()

        threading.Thread(
            target=self._scheduler,
            name="mailflow-scheduler",
            daemon=True,
        ).start()

        logger.info(
            "[STARTUP] workers and scheduler started; "
            "no campaign automatically started"
        )

    def enqueue(self, campaign_id):
        with self.lock:
            if campaign_id in self.enqueued:
                return

            self.enqueued.add(
                campaign_id
            )

        try:
            self.campaign_queue.put_nowait(
                campaign_id
            )

        except queue.Full:
            with self.lock:
                self.enqueued.discard(
                    campaign_id
                )

            raise RuntimeError(
                "Campaign queue is busy; "
                "please try again shortly."
            )

    def _dispatcher(self):
        while True:
            campaign_id = (
                self.campaign_queue.get()
            )

            try:
                for recipient_id in (
                    database.queued_recipient_ids(
                        campaign_id
                    )
                ):
                    self.recipient_queue.put(
                        recipient_id
                    )

            finally:
                with self.lock:
                    self.enqueued.discard(
                        campaign_id
                    )

                self.campaign_queue.task_done()

    def _scheduler(self):
        while True:
            try:
                for campaign_id in (
                    database.activate_due_campaigns()
                ):
                    self.enqueue(
                        campaign_id
                    )

                for campaign_id in (
                    database.activate_due_retries_and_resumes()
                ):
                    self.enqueue(
                        campaign_id
                    )

                database.cleanup_expired_attachment_files(
                    Config.ATTACHMENT_RETENTION_SECONDS
                )

            except Exception:
                logger.exception(
                    "Scheduled campaign check failed"
                )

            time.sleep(5)

    def _payload(self, campaign_id):
        with self.payload_lock:
            if campaign_id not in self.payload_cache:
                campaign, attachments = (
                    database.campaign_payload(
                        campaign_id
                    )
                )

                prepared = []

                for attachment in attachments:
                    if "content" in attachment:
                        prepared.append(
                            attachment
                        )
                        continue

                    try:
                        with open(
                            attachment["file_path"],
                            "rb",
                        ) as source:
                            prepared.append(
                                {
                                    **attachment,
                                    "content": source.read(),
                                }
                            )

                    except OSError as exc:
                        raise RuntimeError(
                            f"Attachment "
                            f"'{attachment['filename']}' "
                            f"cannot be read: {exc}"
                        ) from exc

                self.payload_cache[
                    campaign_id
                ] = (
                    campaign,
                    prepared,
                )

            return self.payload_cache[
                campaign_id
            ]

    def _release_payload_if_finished(
        self,
        campaign_id,
    ):
        campaign = database.get_campaign(
            campaign_id,
            include_failed=False,
        )

        if (
            campaign
            and campaign["status"]
            in database.TERMINAL_CAMPAIGN_STATUSES
        ):
            with self.payload_lock:
                self.payload_cache.pop(
                    campaign_id,
                    None,
                )

    def _wait_for_rate_limit(self):
        if not Config.EMAIL_RATE_LIMIT:
            return

        with self.rate_lock:
            wait = (
                self.last_send
                + 1 / Config.EMAIL_RATE_LIMIT
                - time.monotonic()
            )

            if wait > 0:
                time.sleep(wait)

            self.last_send = time.monotonic()

    @staticmethod
    def _error_details(exc):
        if isinstance(
            exc,
            smtplib.SMTPAuthenticationError,
        ):
            return (
                "SMTP authentication failed",
                False,
                True,
            )

        if isinstance(
            exc,
            smtplib.SMTPRecipientsRefused,
        ):
            return (
                "Recipient rejected",
                False,
                False,
            )

        if isinstance(
            exc,
            smtplib.SMTPSenderRefused,
        ):
            return (
                "SMTP sender address rejected. "
                "Verify the authenticated Zoho sender address.",
                False,
                True,
            )

        if isinstance(
            exc,
            smtplib.SMTPDataError,
        ):
            if exc.smtp_code in (
                552,
                554,
            ):
                return (
                    "Message exceeds SMTP size limit",
                    False,
                    False,
                )

            return (
                f"SMTP data rejected ({exc.smtp_code})",
                exc.smtp_code
                in (
                    421,
                    450,
                    451,
                    452,
                ),
                False,
            )

        if isinstance(
            exc,
            (
                smtplib.SMTPServerDisconnected,
                smtplib.SMTPConnectError,
                socket.timeout,
                TimeoutError,
                ConnectionError,
                OSError,
            ),
        ):
            return (
                "SMTP connection lost or timed out",
                True,
                False,
            )

        if isinstance(
            exc,
            smtplib.SMTPResponseException,
        ):
            return (
                f"SMTP server rejected message "
                f"({exc.smtp_code})",
                exc.smtp_code
                in (
                    421,
                    450,
                    451,
                    452,
                ),
                False,
            )

        return (
            "SMTP server error",
            True,
            False,
        )

    @staticmethod
    def _connection_failed(exc):
        """
        Determine whether the SMTP transport itself failed.

        Recipient-level rejection must NOT destroy a healthy connection.
        """

        if isinstance(
            exc,
            smtplib.SMTPException,
        ):
            return isinstance(
                exc,
                (
                    smtplib.SMTPServerDisconnected,
                    smtplib.SMTPConnectError,
                ),
            )

        return isinstance(
            exc,
            (
                socket.timeout,
                TimeoutError,
                ConnectionError,
                OSError,
            ),
        )

    def _worker(self):
        connection = SMTPConnectionManager()
        builder = EmailMessageBuilder()

        worker_id = (
            threading.current_thread().name
        )

        while True:
            recipient_id = (
                self.recipient_queue.get()
            )

            claimed = None

            try:
                claimed = (
                    database.claim_recipient(
                        recipient_id
                    )
                )

                if not claimed:
                    continue

                campaign, attachments = (
                    self._payload(
                        claimed["campaign_id"]
                    )
                )

                # ------------------------------------------------------
                # PRE-SEND SIZE VALIDATION
                # ------------------------------------------------------
                estimated_size = (
                    estimate_message_size(
                        campaign["body"],
                        attachments,
                        builder.logo_size,
                    )
                )

                if (
                    estimated_size
                    > Config.SMTP_MAX_MESSAGE_SIZE
                ):
                    database.recipient_result(
                        recipient_id,
                        "failed",
                        "Message exceeds configured SMTP size limit",
                    )
                    continue

                # ------------------------------------------------------
                # BUILD MESSAGE
                # ------------------------------------------------------
                build_started = time.monotonic()

                sender = Config.smtp_username()

                message = builder.build(
                    sender,
                    claimed["email"],
                    campaign["subject"],
                    campaign["body"],
                    attachments,
                )

                message_bytes = (
                    message.as_bytes()
                )

                build_elapsed = (
                    time.monotonic()
                    - build_started
                )

                if (
                    len(message_bytes)
                    > Config.SMTP_MAX_MESSAGE_SIZE
                ):
                    database.recipient_result(
                        recipient_id,
                        "failed",
                        "Generated MIME message exceeds "
                        "configured SMTP size limit",
                    )
                    continue

                logger.info(
                    "[PERF] campaign=%s "
                    "build_and_encode=%.2fs "
                    "size=%.2fMB",
                    claimed["campaign_id"],
                    build_elapsed,
                    len(message_bytes)
                    / 1024
                    / 1024,
                )

                attempt = database.record_attempt(
                    recipient_id
                )

                send_started = time.monotonic()

                try:
                    logger.info(
                        "[SMTP] worker=%s "
                        "recipient=%s "
                        "action=SEND "
                        "attempt=%s",
                        worker_id,
                        _redacted_recipient(
                            claimed["email"]
                        ),
                        attempt,
                    )

                    self._wait_for_rate_limit()

                    connection.send_message(
                        message_bytes,
                        claimed["email"],
                        bool(attachments),
                    )

                    database.recipient_result(
                        recipient_id,
                        "sent",
                    )

                    logger.info(
                        "[SMTP] worker=%s "
                        "recipient=%s "
                        "action=SUCCESS "
                        "elapsed=%.2fs",
                        worker_id,
                        _redacted_recipient(
                            claimed["email"]
                        ),
                        time.monotonic()
                        - send_started,
                    )

                except (
                    smtplib.SMTPException,
                    OSError,
                    socket.timeout,
                    TimeoutError,
                    ConnectionError,
                ) as exc:

                    reason, retryable, fatal_auth = (
                        self._error_details(
                            exc
                        )
                    )

                    connection_failed = (
                        self._connection_failed(
                            exc
                        )
                    )

                    if connection_failed:
                        logger.warning(
                            "[SMTP] worker=%s "
                            "action=DISCONNECTED "
                            "stage=%s "
                            "campaign=%s "
                            "attempt=%s "
                            "elapsed=%.2fs "
                            "error=%s "
                            "reason=%s",
                            worker_id,
                            connection.stage,
                            claimed["campaign_id"],
                            attempt,
                            time.monotonic()
                            - send_started,
                            type(exc).__name__,
                            reason,
                        )

                        connection.discard()

                        logger.info(
                            "[SMTP] worker=%s "
                            "action=INVALIDATE_CONNECTION",
                            worker_id,
                        )

                    else:
                        logger.warning(
                            "[SMTP] worker=%s "
                            "action=SEND_ERROR "
                            "stage=%s "
                            "campaign=%s "
                            "attempt=%s "
                            "elapsed=%.2fs "
                            "error=%s "
                            "reason=%s",
                            worker_id,
                            connection.stage,
                            claimed["campaign_id"],
                            attempt,
                            time.monotonic()
                            - send_started,
                            type(exc).__name__,
                            reason,
                        )

                    if fatal_auth:
                        database.fail_campaign_configuration(
                            claimed["campaign_id"],
                            reason,
                        )

                    elif (
                        not retryable
                        or attempt
                        >= Config.MAX_EMAIL_RETRIES + 1
                    ):
                        database.recipient_result(
                            recipient_id,
                            "failed",
                            reason,
                        )

                        logger.info(
                            "[SMTP] worker=%s "
                            "recipient=%s "
                            "action=FAILED",
                            worker_id,
                            _redacted_recipient(
                                claimed["email"]
                            ),
                        )

                    else:
                        # --------------------------------------------------
                        # EXPONENTIAL BACKOFF
                        #
                        # Attempt 1 -> base delay
                        # Attempt 2 -> 2x base delay
                        # Attempt 3 -> 4x base delay
                        # --------------------------------------------------
                        delay = (
                            Config.RETRY_BASE_DELAY
                            * (
                                2
                                ** (attempt - 1)
                            )
                        )

                        database.schedule_retry(
                            recipient_id,
                            reason,
                            delay,
                        )

                        logger.info(
                            "[SMTP] worker=%s "
                            "recipient=%s "
                            "action=RETRY "
                            "next_attempt=%s "
                            "delay=%.2fs",
                            worker_id,
                            _redacted_recipient(
                                claimed["email"]
                            ),
                            attempt + 1,
                            delay,
                        )

                    logger.info(
                        "[SMTP] worker=%s "
                        "action=CONTINUE_NEXT_RECIPIENT",
                        worker_id,
                    )

                except Exception:
                    logger.exception(
                        "Unexpected send failure "
                        "in campaign=%s",
                        claimed["campaign_id"],
                    )

                    database.recipient_result(
                        recipient_id,
                        "failed",
                        "Unexpected application error",
                    )

            except (
                SMTPConfigurationError,
                ValueError,
            ) as exc:

                logger.error(
                    "SMTP configuration error: %s",
                    exc,
                )

                if claimed:
                    database.fail_campaign_configuration(
                        claimed["campaign_id"],
                        f"SMTP sender configuration error: {exc}",
                    )

            except Exception as exc:
                # Payload/file failures occur before the SMTP send loop.
                # They must never strand a recipient in 'sending'.
                logger.exception(
                    "campaign recipient could not be prepared "
                    "(campaign=%s)",
                    claimed["campaign_id"]
                    if claimed
                    else "unknown",
                )

                if claimed:
                    database.recipient_result(
                        recipient_id,
                        "failed",
                        f"Attachment or campaign preparation failed: {exc}",
                    )

            finally:
                if claimed:
                    self._release_payload_if_finished(
                        claimed["campaign_id"]
                    )

                self.recipient_queue.task_done()


campaign_manager = CampaignManager()