import unittest

from app.notifications import delivery_accepted, delivery_body_is_error


class DeliveryBodyTests(unittest.TestCase):
    def test_slack_invalid_token_text_counts_as_failure(self):
        self.assertTrue(delivery_body_is_error(b"invalid_token\n"))

    def test_json_error_payload_counts_as_failure(self):
        self.assertTrue(delivery_body_is_error(b'{"ok": false, "error": "invalid_token"}'))
        self.assertTrue(delivery_body_is_error(b'{"error": "channel_not_found"}'))

    def test_successful_bodies_pass(self):
        self.assertFalse(delivery_body_is_error(b""))
        self.assertFalse(delivery_body_is_error(b"ok"))
        self.assertFalse(delivery_body_is_error(b'{"ok": true, "result": {"message_id": 1}}'))
        self.assertFalse(delivery_body_is_error(b'{"status": "ok", "error": null}'))

    def test_unparseable_body_is_not_a_failure(self):
        self.assertFalse(delivery_body_is_error(b"{not json"))


class DeliveryAcceptanceTests(unittest.TestCase):
    def test_slack_requires_the_ok_acknowledgement(self):
        self.assertTrue(delivery_accepted("slack", 200, b"ok"))
        self.assertFalse(delivery_accepted("slack", 200, b"invalid_token"))
        self.assertFalse(delivery_accepted("slack", 200, b"<html>slack</html>"))
        self.assertFalse(delivery_accepted("slack", 404, b"ok"))

    def test_discord_accepts_an_empty_success(self):
        self.assertTrue(delivery_accepted("discord", 204, b""))
        self.assertFalse(delivery_accepted("discord", 404, b'{"message": "Unknown Webhook"}'))

    def test_telegram_requires_ok_true(self):
        self.assertTrue(delivery_accepted("telegram", 200, b'{"ok": true, "result": {}}'))
        self.assertFalse(delivery_accepted("telegram", 200, b'{"ok": false, "description": "bad token"}'))
        self.assertFalse(delivery_accepted("telegram", 200, b"ok"))

    def test_custom_webhook_accepts_any_clean_2xx(self):
        self.assertTrue(delivery_accepted("webhook", 202, b'{"status": "queued"}'))
        self.assertFalse(delivery_accepted("webhook", 500, b'{"status": "queued"}'))
        self.assertFalse(delivery_accepted(None, 200, b"invalid_token"))


if __name__ == "__main__":
    unittest.main()
