import asyncio
import unittest
from datetime import datetime, timezone

from app.api import iso_timestamp, mark_utc
from app.realtime import DashboardHub


class DashboardHubTests(unittest.TestCase):
    def test_publish_reaches_subscribers(self):
        async def scenario():
            hub = DashboardHub()
            hub.bind_loop(asyncio.get_running_loop())
            queue = hub.subscribe()
            published = hub.publish("metrics", {"cpu_percent": 10})
            await asyncio.sleep(0)
            return published, queue.get_nowait()

        published, message = asyncio.run(scenario())

        self.assertTrue(published)
        self.assertEqual(message, ("metrics", {"cpu_percent": 10}))

    def test_events_inside_the_throttle_window_are_dropped(self):
        async def scenario():
            hub = DashboardHub()
            hub.bind_loop(asyncio.get_running_loop())
            hub.subscribe()
            first = hub.publish("security_events", {"category": "failed_login"})
            second = hub.publish("security_events", {"category": "failed_login"})
            await asyncio.sleep(0)
            return first, second

        self.assertEqual(asyncio.run(scenario()), (True, False))

    def test_publish_without_subscribers_reports_nothing_sent(self):
        async def scenario():
            hub = DashboardHub()
            hub.bind_loop(asyncio.get_running_loop())
            return hub.publish("metrics", {})

        self.assertFalse(asyncio.run(scenario()))

    def test_unsubscribe_stops_delivery(self):
        async def scenario():
            hub = DashboardHub()
            hub.bind_loop(asyncio.get_running_loop())
            queue = hub.subscribe()
            hub.unsubscribe(queue)
            published = hub.publish("metrics", {"cpu_percent": 1})
            return published, hub.subscriber_count

        self.assertEqual(asyncio.run(scenario()), (False, 0))


class EventTimestampTests(unittest.TestCase):
    def test_datetimes_keep_the_iso_separator(self):
        moment = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)

        self.assertEqual(iso_timestamp(moment), "2026-11-13T10:00:00+00:00")
        self.assertIsNone(iso_timestamp(None))


class UtcNormalisationTests(unittest.TestCase):
    def test_naive_datetimes_gain_a_utc_offset(self):
        payload = {"metrics": {"timestamp": datetime(2026, 11, 13, 11, 44, 32)}, "rows": [{"timestamp": None}]}

        normalised = mark_utc(payload)

        self.assertEqual(normalised["metrics"]["timestamp"].utcoffset().total_seconds(), 0)
        self.assertIsNone(normalised["rows"][0]["timestamp"])
        self.assertIsNone(payload["metrics"]["timestamp"].tzinfo)


if __name__ == "__main__":
    unittest.main()
