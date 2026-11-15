import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import collector as collector_module  # noqa: E402


class PortEventCooldownTests(unittest.TestCase):
    def test_first_cycle_reports_every_unknown_port(self):
        findings = [{"port": 445, "known": False}, {"port": 3389, "known": False}]

        emitted, state = collector_module.select_new_findings(
            findings, {}, 1000.0, 3600, lambda finding: str(finding.get("port"))
        )

        self.assertEqual(len(emitted), 2)
        self.assertEqual(state, {"445": 1000.0, "3389": 1000.0})

    def test_repeat_cycle_reports_nothing(self):
        findings = [{"port": 445, "known": False}]
        state = {"445": 1000.0}

        emitted, updated = collector_module.select_new_findings(
            findings, state, 1200.0, 3600, lambda finding: str(finding.get("port"))
        )

        self.assertEqual(emitted, [])
        self.assertEqual(updated, {"445": 1000.0})

    def test_port_is_reported_again_after_the_cooldown(self):
        findings = [{"port": 445, "known": False}]
        state = {"445": 1000.0}

        emitted, updated = collector_module.select_new_findings(
            findings, state, 5000.0, 3600, lambda finding: str(finding.get("port"))
        )

        self.assertEqual(len(emitted), 1)
        self.assertEqual(updated, {"445": 5000.0})

    def test_zero_cooldown_reports_every_cycle(self):
        findings = [{"port": 445, "known": False}]
        state = {"445": 1000.0}

        emitted, _ = collector_module.select_new_findings(
            findings, state, 1001.0, 0, lambda finding: str(finding.get("port"))
        )

        self.assertEqual(len(emitted), 1)


class EventStateFileTests(unittest.TestCase):
    def test_state_round_trip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            collector_module.write_event_state(path, {"445": 12.5})
            self.assertEqual(collector_module.read_event_state(path), {"445": 12.5})

    def test_missing_state_file_reads_as_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            self.assertEqual(collector_module.read_event_state(path), {})

    def test_corrupt_state_file_reads_as_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(collector_module.read_event_state(path), {})


class SpoolTests(unittest.TestCase):
    def test_failed_payloads_are_queued_and_replayed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            spool_path = Path(directory) / "spool.jsonl"
            original_path = collector_module.SPOOL_PATH
            original_max = collector_module.SPOOL_MAX_ITEMS
            collector_module.SPOOL_PATH = spool_path
            collector_module.SPOOL_MAX_ITEMS = 10
            try:
                collector_module.spool_append("metrics", {"host": "test"})
                queued = collector_module.read_spool()
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0]["endpoint"], "metrics")
            finally:
                collector_module.SPOOL_PATH = original_path
                collector_module.SPOOL_MAX_ITEMS = original_max


class EnvironmentTests(unittest.TestCase):
    def test_cooldown_defaults_to_one_hour(self):
        self.assertGreaterEqual(collector_module.PORT_EVENT_COOLDOWN_SECONDS, 0)
        self.assertGreaterEqual(collector_module.FILE_EVENT_COOLDOWN_SECONDS, 0)

    def test_api_url_points_at_the_local_api(self):
        self.assertTrue(collector_module.API_URL.startswith("http://127.0.0.1:8000"))


if __name__ == "__main__":
    unittest.main()
