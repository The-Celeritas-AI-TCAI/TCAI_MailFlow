import os
import smtplib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

# Keep the worker unit test runnable without requiring the web application's
# optional environment-loader package to be installed in the test interpreter.
dotenv_stub = types.ModuleType("dotenv")
dotenv_stub.load_dotenv = lambda: None
sys.modules.setdefault("dotenv", dotenv_stub)

import database
from config import Config
from config import normalized_smtp_username
from email_worker import CampaignManager, SMTPConnectionManager


class CampaignFailureIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.original_db = Config.DATABASE_URL
        self.original_retries = Config.MAX_EMAIL_RETRIES
        self.original_username = Config.SMTP_USERNAME
        self.original_password = Config.SMTP_PASSWORD
        Config.DATABASE_URL = f"sqlite:///{self.temp_db.name}"
        Config.MAX_EMAIL_RETRIES = 1
        Config.SMTP_USERNAME = "sender@example.test"
        Config.SMTP_PASSWORD = "test-password"
        database.init_database()

    def tearDown(self):
        Config.DATABASE_URL = self.original_db
        Config.MAX_EMAIL_RETRIES = self.original_retries
        Config.SMTP_USERNAME = self.original_username
        Config.SMTP_PASSWORD = self.original_password
        # The daemon worker can briefly retain SQLite's Windows file handle after
        # queue.join(); a failed cleanup must not invalidate the behavior test.
        try:
            os.unlink(self.temp_db.name)
        except PermissionError:
            pass

    def test_disconnect_for_one_recipient_does_not_block_the_next(self):
        campaign_id = database.create_campaign(
            ["first@example.test", "broken@example.test", "third@example.test"],
            "Subject", "Body", [],
        )
        recipient_ids = database.queued_recipient_ids(campaign_id)
        manager = CampaignManager()
        calls = []

        def fake_send(_, __, recipient, *args):
            calls.append(recipient)
            if recipient == "broken@example.test":
                raise smtplib.SMTPServerDisconnected("forced disconnect")

        with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
            worker = threading.Thread(target=manager._worker, daemon=True)
            worker.start()
            for recipient_id in recipient_ids:
                manager.recipient_queue.put(recipient_id)
            manager.recipient_queue.join()

        campaign = database.get_campaign(campaign_id)
        self.assertEqual(campaign["sent"], 2)
        self.assertEqual(campaign["failed"], 1)
        self.assertEqual(campaign["status"], "partially_failed")
        self.assertEqual(calls.count("broken@example.test"), 2)
        self.assertIn("third@example.test", calls)

    def test_new_attachment_rows_store_only_filesystem_metadata(self):
        attachment = tempfile.NamedTemporaryFile(delete=False)
        attachment.write(b"campaign attachment")
        attachment.close()
        try:
            campaign_id = database.create_campaign(
                ["recipient@example.test"], "Subject", "Body",
                [{"filename": "attachment.txt", "file_path": attachment.name, "size": 19, "mime_type": "text/plain"}],
            )
            with database.connection() as db:
                columns = [row[1] for row in db.execute("PRAGMA table_info(attachments)")]
                row = db.execute("SELECT filename,file_path,size,mime_type FROM attachments WHERE campaign_id=?", (campaign_id,)).fetchone()
            self.assertNotIn("content", columns)
            self.assertEqual(dict(row)["file_path"], attachment.name)
        finally:
            os.unlink(attachment.name)

    def test_timeout_for_one_recipient_does_not_block_the_next(self):
        campaign_id = database.create_campaign(
            ["first@example.test", "timeout@example.test", "third@example.test"],
            "Subject", "Body", [],
        )
        manager, calls = CampaignManager(), []

        def fake_send(_, __, recipient, *args):
            calls.append(recipient)
            if recipient == "timeout@example.test":
                raise TimeoutError("forced timeout")

        with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
            threading.Thread(target=manager._worker, daemon=True).start()
            for recipient_id in database.queued_recipient_ids(campaign_id):
                manager.recipient_queue.put(recipient_id)
            manager.recipient_queue.join()

        campaign = database.get_campaign(campaign_id)
        self.assertEqual((campaign["sent"], campaign["failed"], campaign["status"]), (2, 1, "partially_failed"))
        self.assertEqual(calls.count("timeout@example.test"), 2)
        self.assertIn("third@example.test", calls)

    def test_attachment_campaign_builds_and_sends_each_recipient(self):
        attachment = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        attachment.write(b"attachment content")
        attachment.close()
        try:
            campaign_id = database.create_campaign(
                ["one@example.test", "two@example.test", "three@example.test"], "Subject", "Body",
                [{"filename": "attachment.txt", "file_path": attachment.name, "size": 18, "mime_type": "text/plain"}],
            )
            manager, calls = CampaignManager(), []

            def fake_send(_, message_bytes, recipient, has_attachments=False):
                self.assertTrue(has_attachments)
                self.assertIn(b"attachment.txt", message_bytes)
                calls.append(recipient)

            with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
                threading.Thread(target=manager._worker, daemon=True).start()
                for recipient_id in database.queued_recipient_ids(campaign_id):
                    manager.recipient_queue.put(recipient_id)
                manager.recipient_queue.join()

            campaign = database.get_campaign(campaign_id)
            self.assertEqual((campaign["sent"], campaign["failed"], campaign["status"]), (3, 0, "completed"))
            self.assertEqual(calls, ["one@example.test", "two@example.test", "three@example.test"])
        finally:
            os.unlink(attachment.name)

    def test_sender_rejection_fails_campaign_without_retrying_each_recipient(self):
        campaign_id = database.create_campaign(
            ["one@example.test", "two@example.test", "three@example.test"], "Subject", "Body", [],
        )
        manager, calls = CampaignManager(), []

        def fake_send(_, __, recipient, *args):
            calls.append(recipient)
            raise smtplib.SMTPSenderRefused(501, b"syntax error", "sender@example.test")

        with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
            threading.Thread(target=manager._worker, daemon=True).start()
            for recipient_id in database.queued_recipient_ids(campaign_id):
                manager.recipient_queue.put(recipient_id)
            manager.recipient_queue.join()

        campaign = database.get_campaign(campaign_id)
        self.assertEqual((campaign["sent"], campaign["failed"], campaign["status"]), (0, 3, "failed"))
        self.assertEqual(calls, ["one@example.test"])
        self.assertIn("SMTP sender address rejected", campaign["fatal_error"])

    def test_send_message_uses_bare_envelope_sender_and_correct_stages(self):
        class FakeSMTP:
            def __init__(self): self.sock = None; self.calls = []
            def has_extn(self, name): return False
            def mail(self, sender, options=()): self.calls.append(("MAIL_FROM", sender, options)); return 250, b"ok"
            def rcpt(self, recipient): self.calls.append(("RCPT_TO", recipient)); return 250, b"ok"
            def data(self, message): self.calls.append(("DATA", message)); return 250, b"ok"

        connection = SMTPConnectionManager()
        connection.smtp = FakeSMTP()
        connection.send_message(b"message", "recipient@example.test")
        self.assertEqual(connection.smtp.calls[0], ("MAIL_FROM", "sender@example.test", []))
        self.assertEqual([call[0] for call in connection.smtp.calls], ["MAIL_FROM", "RCPT_TO", "DATA"])

    def test_sender_rejection_is_identified_as_mail_from(self):
        class RejectingSMTP:
            sock = None
            def has_extn(self, name): return False
            def mail(self, sender, options=()): return 501, b"syntax error in parameters or arguments"

        connection = SMTPConnectionManager()
        connection.smtp = RejectingSMTP()
        with self.assertRaises(smtplib.SMTPSenderRefused):
            connection.send_message(b"message", "recipient@example.test")
        self.assertEqual(connection.stage, "MAIL_FROM")

    def test_smtp_username_rejects_display_name_and_control_characters(self):
        self.assertEqual(normalized_smtp_username(" sender@example.test "), "sender@example.test")
        for invalid in ('Name <sender@example.test>', 'sender@example.test,other@example.test', 'sender@\nexample.test'):
            with self.assertRaises(ValueError):
                normalized_smtp_username(invalid)


if __name__ == "__main__":
    unittest.main()
