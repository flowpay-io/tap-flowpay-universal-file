"""Whole tap runs against an in-memory blob store."""

import json
import logging
import os
import pathlib

import pytest

from tap_flowpay_universal_file.streams import NoWaitingFile
from tap_flowpay_universal_file.tap import TapFlowpayUniversalFile
from tap_flowpay_universal_file.records import InvalidDelivery
from tests.conftest import CSV_HEADER, CSV_PARTNER, jsonl, payment, txn

INBOUND = "inbound-container"
HISTORY = "history-container"
HISTORY_PREFIX = "history/tenant1/gokasa/"
MERCHANT = "M001"
FOLDER = f"inbound/{MERCHANT}/"
CONFIG = {
    "account_url": "https://example.blob.core.windows.net",
    "merchant_id": MERCHANT,
    "inbound_container": INBOUND,
    "inbound_sas_token": "sv=inbound",
    "history_container": HISTORY,
    "history_sas_token": "sv=history",
    "history_prefix": HISTORY_PREFIX,
}


def run_tap(store, capsys, **config):
    """Run a full sync; return the SCHEMA message and the records."""
    tap = TapFlowpayUniversalFile(config={**CONFIG, **config})
    tap.store = store
    tap.sync_all()
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    [schema] = [m["schema"] for m in messages if m["type"] == "SCHEMA"]
    return schema, [m["record"] for m in messages if m["type"] == "RECORD"]


def file_name(number=1):
    return f"{FOLDER}transactions_{MERCHANT}_{number}.jsonl"


def test_initial_file_becomes_the_history_unchanged(store, capsys):
    records = [
        {**txn("a", date="2026-09-01"), "payments": [payment(1000), payment(2500, "CREDIT_CARD")]},
        txn("b", 5000, date="2026-09-02", checkout_id="1000101", cancel=1000102),
        txn("c", -5000, date="2026-09-02", checkout_id="1000102"),
    ]
    store.put(INBOUND, file_name(), jsonl(*records))

    schema, emitted = run_tap(store, capsys)

    assert emitted == records
    assert set(schema["properties"]) == {"transactionId", "payments", "date", "checkoutId", "cancel"}
    assert store.names(INBOUND) == [file_name()]


def test_next_file_merges_into_the_newest_history(store, capsys):
    store.put(HISTORY, HISTORY_PREFIX + "202610/job-a/transactions.jsonl", jsonl(txn("stale")))
    store.put(HISTORY, HISTORY_PREFIX + "202611/job-b/transactions.jsonl",
              jsonl(txn("t1", date="2026-09-01"), txn("t2", date="2026-09-02")))
    store.put(INBOUND, file_name(), jsonl(txn("t2", date="2026-09-02"), txn("t3", date="2026-10-01")))

    _, emitted = run_tap(store, capsys)

    assert [r["transactionId"] for r in emitted] == ["t1", "t2", "t3"]


def test_a_resent_sale_with_cancel_replaces_the_old_version(store, capsys):
    sale = txn("s", 5000, checkout_id="1000101")
    store.put(HISTORY, HISTORY_PREFIX + "202611/job-a/transactions.jsonl", jsonl(sale))
    store.put(INBOUND, file_name(), jsonl({**sale, "cancel": 1000102}))

    _, emitted = run_tap(store, capsys)

    assert emitted == [{**sale, "cancel": 1000102}]


def test_every_waiting_file_is_merged_and_the_newer_one_wins(store, capsys):
    store.put(INBOUND, file_name(2), jsonl(txn("t1", 100)))
    store.put(INBOUND, file_name(1), jsonl(txn("t1", 900), txn("t2", date="2026-09-02")))

    _, emitted = run_tap(store, capsys)

    assert [(r["transactionId"], r["payments"][0]["amount"]) for r in emitted] == [("t1", 900), ("t2", 100)]
    assert store.names(INBOUND) == [file_name(), file_name(2)]


def test_other_merchants_files_are_left_alone(store, capsys):
    store.put(INBOUND, file_name(), jsonl(txn("mine")))
    store.put(INBOUND, "inbound/M002/transactions_M002_1.jsonl", jsonl(txn("theirs")))

    _, emitted = run_tap(store, capsys)

    assert [r["transactionId"] for r in emitted] == ["mine"]
    assert "inbound/M002/transactions_M002_1.jsonl" in store.names(INBOUND)


def test_one_folder_layout_with_the_merchant_id_in_the_file_name(store, capsys):
    store.put(INBOUND, f"inbound/transactions_{MERCHANT}_1.jsonl", jsonl(txn("mine")))
    store.put(INBOUND, "inbound/transactions_M002_1.jsonl", jsonl(txn("theirs")))

    _, emitted = run_tap(store, capsys, inbound_prefix="inbound/transactions_{merchant_id}_")

    assert [r["transactionId"] for r in emitted] == ["mine"]
    assert store.names(INBOUND) == [
        f"inbound/transactions_{MERCHANT}_1.jsonl",
        "inbound/transactions_M002_1.jsonl",
    ]


def test_explicit_archive_false_keeps_existing_manual_configurations_working(store, capsys):
    store.put(INBOUND, file_name(), jsonl(txn("t1")))

    run_tap(store, capsys, archive_processed=False)

    assert store.names(INBOUND) == [file_name()]


def test_no_waiting_file_fails_the_job(store, capsys):
    with pytest.raises(NoWaitingFile):
        run_tap(store, capsys)


def test_bad_file_fails_before_any_record_and_stays_in_inbound(store, capsys):
    store.put(INBOUND, file_name(), jsonl(txn("t1"), {**txn("t2"), "date": "yesterday"}))

    with pytest.raises(InvalidDelivery, match="line 2: date: 'yesterday' is not a 'date'"):
        run_tap(store, capsys)

    assert '"type": "RECORD"' not in capsys.readouterr().out
    assert store.names(INBOUND) == [file_name()]


def test_history_prefix_placeholders_come_from_the_job_environment(store, capsys, monkeypatch):
    monkeypatch.setenv("TENANT", "tenant1")
    monkeypatch.setenv("TAP", "gokasa")
    store.put(HISTORY, "history/tenant1/gokasa/202611/job/transactions.jsonl", jsonl(txn("old", date="2026-08-01")))
    store.put(INBOUND, file_name(), jsonl(txn("new", date="2026-09-01")))

    _, emitted = run_tap(store, capsys, history_prefix="history/{tenant}/{tap}/")

    assert [r["transactionId"] for r in emitted] == ["old", "new"]


def test_a_placeholder_without_its_environment_variable_fails(store, capsys, monkeypatch):
    monkeypatch.delenv("TENANT", raising=False)
    store.put(INBOUND, file_name(), jsonl(txn("t1")))

    with pytest.raises(ValueError, match="TENANT is not set"):
        run_tap(store, capsys, history_prefix="history/{tenant}/")


@pytest.mark.skipif(not os.environ.get("GOKASA_SAMPLE"), reason="set GOKASA_SAMPLE to a GoKasa export")
def test_real_export_passes_through_and_a_second_run_changes_nothing(store, capsys):
    path = pathlib.Path(os.environ["GOKASA_SAMPLE"])
    content = path.read_bytes()
    expected = sorted(
        (json.loads(line) for line in content.decode("utf-8-sig").split("\n") if line.strip()),
        key=lambda r: (r["date"], r["transactionId"]),
    )
    # Use a synthetic filename in the expected partner format; fixture contents stay private.
    store.put(INBOUND, file_name(), content)

    _, first = run_tap(store, capsys, input_blob=file_name(), history_blob=None)
    assert first == expected

    # The next job reads that output back as history and gets the same file again.
    previous = HISTORY_PREFIX + "202611/job-1/transactions.jsonl"
    store.put(HISTORY, previous, jsonl(*first))
    _, second = run_tap(store, capsys, input_blob=file_name(), history_blob=previous)
    assert second == expected
    assert store.read(INBOUND, file_name()) == content


def test_a_field_that_is_not_configured_is_ignored_and_logged_by_name_only(store, capsys, caplog, monkeypatch):
    # The SDK's logger does not propagate to pytest's handler, so attach it directly.
    logger = logging.getLogger("tap-flowpay-universal-file")
    monkeypatch.setattr(logger, "handlers", [*logger.handlers, caplog.handler])
    store.put(INBOUND, file_name(), jsonl({**txn("t1"), "cashier": "Alice Example"}))

    _, emitted = run_tap(store, capsys, input_blob=file_name(), history_blob=None)

    assert emitted == [txn("t1")]
    assert "cashier (1 records)" in caplog.text
    assert "Alice Example" not in caplog.text


def test_a_csv_partner_works_from_settings_alone(store, capsys):
    settings = {**CSV_PARTNER, "history_prefix": HISTORY_PREFIX}
    first = f"{FOLDER}transactions_{MERCHANT}_1.csv"
    store.put(INBOUND, first, CSV_HEADER + "R2;1;2026-09-02 09:00;10,5;;;Bob\nR1;1;2026-09-01 10:00;3;;;Bob\n")

    schema, initial = run_tap(store, capsys, **settings, input_blob=first, history_blob=None)

    assert set(schema["properties"]) == set(CSV_PARTNER["schema"]["properties"])
    assert initial == [
        {"receiptId": "R1", "line": 1, "soldAt": "2026-09-01 10:00", "amount": 3},
        {"receiptId": "R2", "line": 1, "soldAt": "2026-09-02 09:00", "amount": 10.5},
    ]

    history = HISTORY_PREFIX + "202611/job-1/transactions.jsonl"
    store.put(HISTORY, history, jsonl(*initial))
    second = f"{FOLDER}transactions_{MERCHANT}_2.csv"
    store.put(INBOUND, second, CSV_HEADER + "R1;1;2026-09-01 10:00;4;true;corrected;\n")

    _, merged = run_tap(store, capsys, **settings, input_blob=second, history_blob=history)

    assert merged == [
        {"receiptId": "R1", "line": 1, "soldAt": "2026-09-01 10:00", "amount": 4, "refund": True, "note": "corrected"},
        initial[1],
    ]


def gokasa_settings():
    from tap_flowpay_universal_file.contract import GOKASA_SETTINGS
    return GOKASA_SETTINGS


def test_explicit_gokasa_settings_emit_exactly_what_the_preset_emits(store, capsys):
    records = [
        {**txn("a", date="2026-09-02"), "payments": [payment(1000), payment(2500, "CREDIT_CARD")]},
        txn("b", 5000, date="2026-09-01", checkout_id="1000101", cancel=1000102),
        txn("c", -5000, date="2026-09-02", checkout_id="1000102"),
    ]
    store.put(INBOUND, file_name(), jsonl(*records))

    preset_schema, preset = run_tap(store, capsys, input_blob=file_name(), history_blob=None)
    explicit_schema, explicit = run_tap(store, capsys, **gokasa_settings(), input_blob=file_name(), history_blob=None)

    assert explicit == preset
    assert explicit_schema == preset_schema


@pytest.mark.skipif(not os.environ.get("GOKASA_SAMPLE"), reason="set GOKASA_SAMPLE to a GoKasa export")
def test_real_export_through_explicit_settings_matches_the_preset(store, capsys):
    content = pathlib.Path(os.environ["GOKASA_SAMPLE"]).read_bytes()
    store.put(INBOUND, file_name(), content)

    _, preset = run_tap(store, capsys, input_blob=file_name(), history_blob=None)
    _, explicit = run_tap(store, capsys, **gokasa_settings(), input_blob=file_name(), history_blob=None)

    assert explicit == preset
