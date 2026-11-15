import unittest

from app.schemas import AlertRuleCreate, EffectiveThresholds, ThresholdsUpdate


class AlertRuleScopeTests(unittest.TestCase):
    def test_host_defaults_to_all_hosts(self):
        rule = AlertRuleCreate(name="cpu", metric="cpu_percent", operator=">", threshold=90)

        self.assertEqual(rule.host, "")

    def test_host_is_trimmed(self):
        rule = AlertRuleCreate(
            name="cpu", metric="cpu_percent", operator=">", threshold=90, host="  web-01 "
        )

        self.assertEqual(rule.host, "web-01")


class ThresholdsScopeTests(unittest.TestCase):
    def test_host_scoped_thresholds_accept_valid_values(self):
        thresholds = ThresholdsUpdate(
            cpu_percent=75,
            memory_percent=80,
            disk_percent=85,
            failed_login_count=3,
            failed_login_window_minutes=5,
        )
        effective = EffectiveThresholds(
            scope="web-01", overridden=True, **thresholds.model_dump()
        )

        self.assertEqual(effective.scope, "web-01")
        self.assertTrue(effective.overridden)
        self.assertEqual(effective.cpu_percent, 75)

    def test_thresholds_reject_out_of_range_values(self):
        with self.assertRaises(ValueError):
            ThresholdsUpdate(
                cpu_percent=120,
                memory_percent=80,
                disk_percent=85,
                failed_login_count=3,
                failed_login_window_minutes=5,
            )


if __name__ == "__main__":
    unittest.main()
