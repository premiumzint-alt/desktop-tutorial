import unittest

from app.database import Base
from app.models import RetentionSetting
from app.retention import (
    SECONDS_PER_DAY,
    apply_retention,
    ensure_ttl_index,
    retention_days,
)
from app.schemas import RetentionSettings
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


class FakeCollection:
    def __init__(self, indexes):
        self.indexes = indexes
        self.created = []
        self.dropped = []

    def list_indexes(self):
        return list(self.indexes)

    def drop_index(self, name):
        self.dropped.append(name)

    def create_index(self, keys, **options):
        self.created.append((keys, options))


class RetentionTests(unittest.TestCase):
    def test_creates_ttl_index_when_missing(self):
        collection = FakeCollection([{"name": "_id_", "key": {"_id": 1}}])

        result = ensure_ttl_index(collection, 7)

        self.assertEqual(result, "created")
        self.assertEqual(collection.created, [("timestamp", {"expireAfterSeconds": 7 * SECONDS_PER_DAY})])
        self.assertEqual(collection.dropped, [])

    def test_plain_timestamp_index_is_replaced(self):
        collection = FakeCollection([{"name": "timestamp_1", "key": {"timestamp": 1}}])

        result = ensure_ttl_index(collection, 30)

        self.assertEqual(result, "created")
        self.assertEqual(collection.dropped, ["timestamp_1"])
        self.assertEqual(collection.created[0][1]["expireAfterSeconds"], 30 * SECONDS_PER_DAY)

    def test_matching_ttl_index_is_left_alone(self):
        collection = FakeCollection(
            [{"name": "timestamp_1", "key": {"timestamp": 1}, "expireAfterSeconds": 14 * SECONDS_PER_DAY}]
        )

        result = ensure_ttl_index(collection, 14)

        self.assertEqual(result, "unchanged")
        self.assertEqual(collection.created, [])
        self.assertEqual(collection.dropped, [])

    def test_changed_retention_rebuilds_the_index(self):
        collection = FakeCollection(
            [{"name": "timestamp_1", "key": {"timestamp": 1}, "expireAfterSeconds": 3 * SECONDS_PER_DAY}]
        )

        result = ensure_ttl_index(collection, 7)

        self.assertEqual(result, "created")
        self.assertEqual(collection.dropped, ["timestamp_1"])
        self.assertEqual(collection.created[0][1]["expireAfterSeconds"], 7 * SECONDS_PER_DAY)

    def test_zero_days_disables_retention(self):
        collection = FakeCollection([{"name": "timestamp_1", "key": {"timestamp": 1}}])

        result = ensure_ttl_index(collection, 0)

        self.assertEqual(result, "disabled")
        self.assertEqual(collection.created, [])
        self.assertEqual(collection.dropped, [])

    def test_compound_index_is_not_touched(self):
        collection = FakeCollection([{"name": "host_1_timestamp_-1", "key": {"host": 1, "timestamp": -1}}])

        ensure_ttl_index(collection, 7)

        self.assertEqual(collection.dropped, [])
        self.assertEqual(collection.created[0][0], "timestamp")


class FakeDatabase:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection([]))


class ApplyRetentionTests(unittest.TestCase):
    def test_each_collection_gets_its_own_days(self):
        database = FakeDatabase()

        result = apply_retention(database, {"metrics": 3, "security_events": 0, "scan_results": 45})

        self.assertEqual(result, {"metrics": "created", "security_events": "disabled", "scan_results": "created"})
        self.assertEqual(
            database["metrics"].created[0][1]["expireAfterSeconds"], 3 * SECONDS_PER_DAY
        )
        self.assertEqual(
            database["scan_results"].created[0][1]["expireAfterSeconds"], 45 * SECONDS_PER_DAY
        )

    def test_defaults_come_from_the_environment(self):
        result = apply_retention(FakeDatabase())

        self.assertEqual(set(result), {"metrics", "security_events", "scan_results"})


class RetentionSettingsStorageTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()

    def test_first_read_seeds_the_environment_defaults(self):
        days = retention_days(self.session)

        self.assertEqual(days, {"metrics": 7, "security_events": 30, "scan_results": 30})

    def test_stored_values_are_returned(self):
        retention_days(self.session)
        settings = RetentionSettings(metrics=2, security_events=14, scan_results=0)
        record = self.session.get(RetentionSetting, 1)
        record.metrics_days = settings.metrics
        record.security_events_days = settings.security_events
        record.scan_results_days = settings.scan_results
        self.session.commit()

        self.assertEqual(retention_days(self.session), {"metrics": 2, "security_events": 14, "scan_results": 0})

    def test_schema_rejects_out_of_range_values(self):
        with self.assertRaises(ValueError):
            RetentionSettings(metrics=-1, security_events=30, scan_results=30)
        with self.assertRaises(ValueError):
            RetentionSettings(metrics=7, security_events=30, scan_results=3651)


if __name__ == "__main__":
    unittest.main()
