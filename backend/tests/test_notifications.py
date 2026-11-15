import unittest

from app.notifications import (
    alert_fingerprint,
    alert_message,
    alert_severity,
    build_alert_payload,
    build_service_payload,
    channel_body,
    format_alert_text,
)


class AlertPayloadTests(unittest.TestCase):
    def test_build_alert_payload_includes_summary_and_alerts(self):
        payload = build_alert_payload(
            alerts=[
                {"category": "system_threshold", "metric": "cpu_percent", "value": 96},
                {"category": "failed_login_threshold", "source_ip": "10.0.0.4", "count": 12},
            ],
            metrics={"cpu_percent": 96, "memory_percent": 81, "disk_percent": 66},
            host_name="api-prod-01",
        )

        self.assertEqual(payload["host"], "api-prod-01")
        self.assertEqual(payload["summary"]["total_alerts"], 2)
        self.assertEqual(payload["summary"]["critical_count"], 1)
        self.assertEqual(payload["summary"]["warning_count"], 1)
        self.assertIn("cpu_percent", payload["alerts"][0]["message"])
        self.assertIn("10.0.0.4", payload["alerts"][1]["message"])


class ChannelFormattingTests(unittest.TestCase):
    def payload(self):
        return build_alert_payload(
            alerts=[{"category": "system_threshold", "metric": "cpu_percent", "value": 96}],
            metrics={"cpu_percent": 96, "memory_percent": 81, "disk_percent": 66},
            host_name="api-prod-01",
        )

    def test_alert_text_summarises_host_and_alerts(self):
        text = format_alert_text(self.payload())

        self.assertIn("api-prod-01", text)
        self.assertIn("WARNING", text)
        self.assertIn("cpu_percent", text)

    def test_slack_and_discord_use_text_fields(self):
        payload = self.payload()
        text = format_alert_text(payload)

        self.assertEqual(channel_body("slack", payload), {"text": text})
        self.assertEqual(channel_body("discord", payload), {"content": text})

    def test_telegram_body_includes_chat_id(self):
        body = channel_body("telegram", self.payload(), "12345")

        self.assertEqual(body["chat_id"], "12345")
        self.assertIn("Sentinel", body["text"])

    def test_custom_webhook_receives_structured_payload(self):
        payload = self.payload()

        self.assertIs(channel_body("webhook", payload), payload)


class ServicePayloadTests(unittest.TestCase):
    def test_service_payload_renders_a_watchdog_message(self):
        payload = build_service_payload("web-01", "Dashboard restarted by the watchdog")
        text = format_alert_text(payload)

        self.assertEqual(payload["host"], "web-01")
        self.assertEqual(payload["summary"]["total_alerts"], 1)
        self.assertEqual(payload["summary"]["status"], "warning")
        self.assertIn("service_watchdog", text)
        self.assertIn("Dashboard restarted by the watchdog", text)

    def test_service_payload_marks_critical_severity(self):
        payload = build_service_payload("web-01", "Collector could not restart", "critical")

        self.assertEqual(payload["summary"]["status"], "critical")
        self.assertEqual(payload["summary"]["critical_count"], 1)
        self.assertIn("CRITICAL", format_alert_text(payload))


class AlertIdentityTests(unittest.TestCase):
    def test_fingerprint_is_stable_for_the_same_alert(self):
        first = {"category": "system_threshold", "metric": "cpu_percent", "value": 96}
        later = {"category": "system_threshold", "metric": "cpu_percent", "value": 99}

        self.assertEqual(alert_fingerprint(first), alert_fingerprint(later))

    def test_fingerprint_separates_different_alerts(self):
        cpu = {"category": "system_threshold", "metric": "cpu_percent", "value": 96}
        disk = {"category": "system_threshold", "metric": "disk_percent", "value": 96}
        login = {"category": "failed_login_threshold", "source_ip": "10.0.0.4", "count": 12}

        self.assertNotEqual(alert_fingerprint(cpu), alert_fingerprint(disk))
        self.assertNotEqual(alert_fingerprint(cpu), alert_fingerprint(login))

    def test_fingerprint_separates_hosts(self):
        first = {"host": "web-01", "category": "system_threshold", "metric": "cpu_percent", "value": 96}
        second = {"host": "web-02", "category": "system_threshold", "metric": "cpu_percent", "value": 96}

        self.assertNotEqual(alert_fingerprint(first), alert_fingerprint(second))
        self.assertEqual(alert_fingerprint(first), alert_fingerprint(dict(first, value=99)))

    def test_severity_and_message_are_derived_from_the_category(self):
        login = {"category": "failed_login_threshold", "source_ip": "10.0.0.4", "count": 12}
        disk = {"category": "system_threshold", "metric": "disk_percent", "value": 95}

        self.assertEqual(alert_severity(login), "critical")
        self.assertEqual(alert_severity(disk), "warning")
        self.assertIn("10.0.0.4", alert_message(login))


if __name__ == "__main__":
    unittest.main()
