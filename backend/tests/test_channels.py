import unittest

from app.schemas import NotificationChannelCreate, NotificationChannelUpdate


class TelegramUrlTests(unittest.TestCase):
    def test_bare_bot_token_becomes_send_message_url(self):
        channel = NotificationChannelCreate(
            name="ops", kind="telegram", webhook_url="123456789:AAF1a2b3c4d5e6f7g8h9i0j", chat_id="-1001234567890"
        )

        self.assertEqual(
            channel.webhook_url,
            "https://api.telegram.org/bot123456789:AAF1a2b3c4d5e6f7g8h9i0j/sendMessage",
        )

    def test_full_url_is_kept(self):
        url = "https://api.telegram.org/bot123456789:AAF1a2b3c4d5e6f7g8h9i0j/sendMessage"
        channel = NotificationChannelCreate(name="ops", kind="telegram", webhook_url=url, chat_id="1")

        self.assertEqual(channel.webhook_url, url)

    def test_update_accepts_a_bare_token(self):
        update = NotificationChannelUpdate(webhook_url="123456789:AAF1a2b3c4d5e6f7g8h9i0j")

        self.assertTrue(update.webhook_url.endswith("/sendMessage"))

    def test_webhook_url_still_validated(self):
        with self.assertRaises(ValueError):
            NotificationChannelCreate(name="ops", kind="webhook", webhook_url="not-a-url")


if __name__ == "__main__":
    unittest.main()
