import unittest
from datetime import datetime, timedelta, timezone

from app import api
from app.config import settings
from app.models import User


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        api.LOGIN_ATTEMPTS.clear()

    def test_allows_attempts_up_to_the_limit(self):
        for _ in range(settings.login_max_per_window):
            self.assertFalse(api.login_throttled("203.0.113.9"))
        self.assertTrue(api.login_throttled("203.0.113.9"))

    def test_limits_are_tracked_per_client(self):
        for _ in range(settings.login_max_per_window):
            api.login_throttled("198.51.100.1")

        self.assertFalse(api.login_throttled("198.51.100.2"))


class AccountLockTests(unittest.TestCase):
    def make_user(self, locked_until=None):
        return User(id=1, username="admin", hashed_password="x", locked_until=locked_until)

    def test_lock_reports_remaining_seconds(self):
        now = datetime.now(timezone.utc)

        remaining = api.locked_seconds_remaining(self.make_user(now + timedelta(minutes=5)), now)

        self.assertGreater(remaining, 240)
        self.assertLessEqual(remaining, 300)

    def test_unlocked_account_has_no_wait(self):
        self.assertEqual(api.locked_seconds_remaining(self.make_user(), datetime.now(timezone.utc)), 0)

    def test_expired_lock_is_ignored(self):
        now = datetime.now(timezone.utc)

        self.assertEqual(
            api.locked_seconds_remaining(self.make_user(now - timedelta(seconds=1)), now),
            0,
        )


if __name__ == "__main__":
    unittest.main()
