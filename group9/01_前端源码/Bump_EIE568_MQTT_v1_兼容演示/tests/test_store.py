import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from bump.protocol import EventConflictError, EventValidationError
from bump.store import Store, StoreError


def make_settings(tmp_path):
    return {
        "db_path": str(tmp_path / "events.sqlite3"),
        "users": [
            {"id": name, "password": f"{name}-test-password",
             "display_name": name.title(), "bio": f"{name} private bio",
             "contact": f"{name}@example.invalid"}
            for name in ("alice", "bob", "charlie")
        ],
        "devices": {
            "badge-a": {"owner": "alice", "alias": "A00001"},
            "badge-b": {"owner": "bob", "alias": "B00002"},
        },
        "sessions": {"demo-001": {"A00001": "badge-a", "B00002": "badge-b"}},
    }


def event(device="badge-a", **changes):
    data = {
        "schema_version": 1, "device_id": device, "boot_id": f"{device}-boot",
        "seq": 1, "session_id": "demo-001",
        "peer_alias": "B00002" if device == "badge-a" else "A00001",
        "start_ms": 1000, "end_ms": 7000,
        "start_utc": "2026-10-23T08:00:00Z",
        "end_utc": "2026-10-23T08:00:06Z",
        "time_quality": "synced", "rssi_median": -55,
        "sample_count": 12, "source": "simulated",
    }
    data.update(changes)
    return data


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def store(settings):
    return Store(settings)


def test_commit_duplicate_timestamp_normalization_and_conflict(store, settings):
    original = event()
    before = copy.deepcopy(original)
    ack = store.ingest_event(original, "badge-a")
    assert ack == {"schema_version": 1, "device_id": "badge-a",
                   "boot_id": "badge-a-boot", "seq": 1, "status": "stored"}
    assert original == before
    equivalent = event(start_utc="2026-10-23T16:00:00+08:00",
                       end_utc="2026-10-23T16:00:06+08:00", rssi_median=-55.0)
    assert store.ingest_event(equivalent, "badge-a")["status"] == "duplicate"
    with pytest.raises(EventConflictError):
        store.ingest_event(event(rssi_median=-56), "badge-a")
    reopened = Store(settings)
    assert len(reopened.list_encounters("alice")) == 1
    assert reopened.list_encounters("alice")[0]["start_utc"] == "2026-10-23T08:00:00.000000Z"


@pytest.mark.parametrize("change", [
    {"extra": "not allowed"},
    {"schema_version": True},
    {"schema_version": 2},
    {"seq": True},
    {"seq": 0},
    {"sample_count": 0},
    {"start_ms": -1},
    {"end_ms": 999},
    {"rssi_median": float("nan")},
    {"rssi_median": -128},
    {"rssi_median": 10**400},
    {"boot_id": ""},
    {"session_id": "absent"},
    {"peer_alias": "A00001"},
    {"peer_alias": "absent"},
    {"source": "unverified"},
    {"time_quality": "unknown"},
    {"time_quality": "unverified"},
    {"start_utc": "2026-10-23T08:00:00"},
    {"start_utc": "2026-10-24T08:00:00Z"},
    {"end_utc": "2026-10-23T08:00:08Z"},
])
def test_invalid_events_leave_no_rows(store, change):
    with pytest.raises(EventValidationError):
        store.ingest_event(event(**change), "badge-a")
    assert store.list_encounters("alice") == []


def test_device_must_match_authenticated_topic(store):
    with pytest.raises(EventValidationError):
        store.ingest_event(event(), "badge-b")
    assert store.list_encounters("alice") == []


def test_both_observers_must_be_members_of_session(store, settings):
    settings["sessions"]["demo-001"] = {"B00002": "badge-b"}
    with pytest.raises(EventValidationError):
        store.ingest_event(event(), "badge-a")


def test_simulated_hardware_and_unknown_time_never_form_false_mutual(store):
    store.ingest_event(event(), "badge-a")
    store.ingest_event(event("badge-b", source="hardware"), "badge-b")
    assert not store.list_encounters("alice")[0]["mutual"]
    assert not store.list_encounters("bob")[0]["mutual"]
    store.ingest_event(event("badge-b", seq=2, time_quality="unknown",
                             start_utc=None, end_utc=None), "badge-b")
    assert not store.list_encounters("alice")[0]["mutual"]
    store.ingest_event(event("badge-b", seq=3, start_utc="2026-10-23T08:01:00Z",
                             end_utc="2026-10-23T08:01:06Z"), "badge-b")
    assert not store.list_encounters("alice")[0]["mutual"]
    store.ingest_event(event("badge-b", seq=4), "badge-b")
    assert store.list_encounters("alice")[0]["mutual"]
    bob = {item["id"]: item for item in store.list_encounters("bob")}
    assert sum(item["mutual"] for item in bob.values()) == 1
    assert len(bob) == 4


def test_soft_hide_keeps_deduplication_and_other_observation(store, settings):
    first = event()
    store.ingest_event(first, "badge-a")
    store.ingest_event(event("badge-b"), "badge-b")
    encounter_id = store.list_encounters("alice")[0]["id"]
    store.hide_encounter("alice", encounter_id)
    store.hide_encounter("alice", encounter_id)
    assert store.ingest_event(first, "badge-a")["status"] == "duplicate"
    assert Store(settings).list_encounters("alice") == []
    assert store.list_encounters("bob")[0]["mutual"]


def test_failed_commit_never_returns_ack_or_persists_event(store, monkeypatch):
    class FailedCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    def fail_connection():
        db = sqlite3.connect(store.path, factory=FailedCommit)
        db.row_factory = sqlite3.Row
        return db

    original = store._connect
    monkeypatch.setattr(store, "_connect", fail_connection)
    with pytest.raises(StoreError):
        store.ingest_event(event(), "badge-a")
    monkeypatch.setattr(store, "_connect", original)
    assert store.list_encounters("alice") == []
    assert store.ingest_event(event(), "badge-a")["status"] == "stored"


def test_concurrent_duplicate_delivery_creates_one_durable_event(store):
    with ThreadPoolExecutor(max_workers=4) as executor:
        acks = list(executor.map(
            lambda _: store.ingest_event(event(), "badge-a"), range(8)))
    assert [ack["status"] for ack in acks].count("stored") == 1
    assert [ack["status"] for ack in acks].count("duplicate") == 7
    assert len(store.list_encounters("alice")) == 1
