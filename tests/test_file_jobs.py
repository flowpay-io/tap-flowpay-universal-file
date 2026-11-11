"""Exact-file jobs must be isolated, repeatable, and leave source blobs untouched."""

import pytest

from tap_gokasa.history import InvalidHistory
from tap_gokasa.tap import TapGoKasa
from tap_gokasa.transactions import InvalidDelivery
from tests.conftest import jsonl, txn
from tests.test_tap import CONFIG, HISTORY, HISTORY_PREFIX, INBOUND, file_name, run_tap


def no_listing(*args, **kwargs):
    raise AssertionError("An exact-file job must not list storage")


def test_exact_job_reads_only_the_named_delivery_and_preserves_all_inputs(store, capsys, monkeypatch):
    selected = "inbound/transactions_M001_delivery-1.jsonl"
    store.put(INBOUND, selected, jsonl(txn("selected")))
    store.put(INBOUND, file_name(2), "not valid JSON")
    store.put(INBOUND, "inbound/transactions_M002_delivery-1.jsonl", jsonl(txn("another-merchant")))
    before = dict(store.blobs)
    monkeypatch.setattr(store, "list_blobs", no_listing)

    _, emitted = run_tap(store, capsys, input_blob=selected, history_blob=None)

    assert emitted == [txn("selected")]
    assert store.blobs == before


def test_exact_job_uses_selected_successful_history_even_when_newer_blobs_exist(store, capsys, monkeypatch):
    history = HISTORY_PREFIX + "202611/success/transactions.jsonl"
    store.put(HISTORY, history, jsonl(txn("old")))
    store.put(HISTORY, HISTORY_PREFIX + "202611/failed/transactions.jsonl", "broken output")
    store.put(INBOUND, file_name(), jsonl(txn("new", date="2026-10-01")))
    monkeypatch.setattr(store, "list_blobs", no_listing)

    _, emitted = run_tap(store, capsys, input_blob=file_name(), history_blob=history)

    assert emitted == [txn("old"), txn("new", date="2026-10-01")]


def test_increment_correction_and_replay_preserve_cancellation_links_and_payments(store, capsys, monkeypatch):
    first = txn("sale", 5000, checkout_id="1000101")
    corrected = {**first, "cancel": 1000102}
    refund = txn("refund", -5000, checkout_id="1000102")
    split = txn("split", 100, 200)
    store.put(INBOUND, file_name(), jsonl(first, split))
    monkeypatch.setattr(store, "list_blobs", no_listing)
    _, initial = run_tap(store, capsys, input_blob=file_name(), history_blob=None)

    history = HISTORY_PREFIX + "202611/job-1/transactions.jsonl"
    store.put(HISTORY, history, jsonl(*initial))
    store.put(INBOUND, file_name(2), jsonl(corrected, refund))
    _, updated = run_tap(store, capsys, input_blob=file_name(2), history_blob=history)
    assert updated == [refund, corrected, split]

    next_history = HISTORY_PREFIX + "202611/job-2/transactions.jsonl"
    store.put(HISTORY, next_history, jsonl(*updated))
    _, replayed = run_tap(store, capsys, input_blob=file_name(2), history_blob=next_history)
    assert replayed == updated
    assert store.names(INBOUND) == [file_name(), file_name(2)]


@pytest.mark.parametrize("blob", [
    "inbound/transactions_M002_1.jsonl",
    "inbound/transactions_M001_extra_1.jsonl",
    "inbound/transactions_M001_.jsonl",
    "inbound/transactions_M001_1.csv",
    "inbound/wrong_M001_1.jsonl",
    "https://example.blob.core.windows.net/inbound/transactions_M001_1.jsonl",
    "/inbound/transactions_M001_1.jsonl",
    "inbound/../transactions_M001_1.jsonl",
    "inbound//transactions_M001_1.jsonl",
    "inbound\\transactions_M001_1.jsonl",
    "inbound/transactions_M001_1.jsonl?sig=do-not-print",
    "inbound/%2e%2e/transactions_M001_1.jsonl",
])
def test_wrong_merchant_or_invalid_input_reference_fails_before_storage(blob, store, capsys, monkeypatch):
    monkeypatch.setattr(store, "list_blobs", no_listing)
    with pytest.raises(ValueError, match="input_blob") as error:
        run_tap(store, capsys, input_blob=blob, history_blob=None)
    assert "do-not-print" not in str(error.value)
    assert '"type": "RECORD"' not in capsys.readouterr().out


@pytest.mark.parametrize("history", [
    "history/tenant2/gokasa/202611/job/transactions.jsonl",
    "history/tenant1/gokasa-other/202611/job/transactions.jsonl",
    HISTORY_PREFIX + "../other/transactions.jsonl",
    HISTORY_PREFIX + "202611/job/orders.jsonl",
    "https://example.blob.core.windows.net/history/transactions.jsonl",
    "",
])
def test_history_must_be_a_supported_file_inside_the_connection_prefix(history, store, capsys):
    with pytest.raises(ValueError, match="history_blob"):
        run_tap(store, capsys, input_blob=file_name(), history_blob=history)


def test_exact_job_requires_an_explicit_first_run_or_previous_output():
    with pytest.raises(ValueError, match="history_blob"):
        TapGoKasa(config={**CONFIG, "input_blob": file_name()})


def test_event_configuration_cannot_fall_back_to_scanning_when_file_override_is_missing():
    with pytest.raises(ValueError, match="input_blob"):
        TapGoKasa(config={**CONFIG, "require_input_blob": True, "history_blob": None})


def test_empty_input_reference_cannot_fall_back_to_scanning():
    with pytest.raises(ValueError, match="input_blob"):
        TapGoKasa(config={**CONFIG, "input_blob": "", "history_blob": None})


def test_missing_input_fails_without_falling_back_to_other_files(store, capsys, monkeypatch):
    store.put(INBOUND, file_name(2), jsonl(txn("unselected")))
    monkeypatch.setattr(store, "list_blobs", no_listing)
    with pytest.raises(KeyError):
        run_tap(store, capsys, input_blob=file_name(), history_blob=None)
    assert '"type": "RECORD"' not in capsys.readouterr().out


def test_missing_selected_history_does_not_reset_history(store, capsys, monkeypatch):
    store.put(INBOUND, file_name(), jsonl(txn("new")))
    monkeypatch.setattr(store, "list_blobs", no_listing)
    with pytest.raises(KeyError):
        run_tap(store, capsys, input_blob=file_name(),
                history_blob=HISTORY_PREFIX + "202611/missing/transactions.jsonl")
    assert '"type": "RECORD"' not in capsys.readouterr().out


@pytest.mark.parametrize("bad_history", [False, True])
def test_invalid_selected_file_emits_no_partial_history(store, capsys, bad_history):
    history = HISTORY_PREFIX + "202611/job/transactions.jsonl"
    store.put(INBOUND, file_name(), jsonl(txn("new")) if bad_history else jsonl(txn("new")) + "broken\n")
    store.put(HISTORY, history, "broken\n" if bad_history else jsonl(txn("old")))
    before = dict(store.blobs)
    with pytest.raises(InvalidHistory if bad_history else InvalidDelivery):
        run_tap(store, capsys, input_blob=file_name(), history_blob=history)
    assert '"type": "RECORD"' not in capsys.readouterr().out
    assert store.blobs == before


def test_archive_configuration_cannot_delete_source_files():
    with pytest.raises(ValueError, match="archive_processed"):
        TapGoKasa(config={**CONFIG, "archive_processed": True})


def test_unknown_partner_cannot_fall_back_to_the_gokasa_parser():
    with pytest.raises(ValueError, match="partner_code"):
        TapGoKasa(config={**CONFIG, "partner_code": "unsupported-partner"})


def test_history_prefix_without_trailing_slash_still_has_a_folder_boundary(store, capsys):
    store.put(INBOUND, file_name(), jsonl(txn("mine")))
    store.put(HISTORY, HISTORY_PREFIX.rstrip("/") + "-other/transactions.jsonl", jsonl(txn("theirs")))
    _, emitted = run_tap(store, capsys, history_prefix=HISTORY_PREFIX.rstrip("/"))
    assert emitted == [txn("mine")]


def test_exact_job_resolves_the_history_connection_from_environment(store, capsys, monkeypatch):
    monkeypatch.setenv("TENANT", "tenant1")
    monkeypatch.setenv("TAP", "gokasa")
    history = HISTORY_PREFIX + "202611/job/transactions.jsonl"
    store.put(INBOUND, "transactions_M001_first.jsonl", jsonl(txn("new")))
    store.put(HISTORY, history, jsonl(txn("old")))
    monkeypatch.setattr(store, "list_blobs", no_listing)
    _, emitted = run_tap(store, capsys, input_blob="transactions_M001_first.jsonl",
                         history_blob=history, history_prefix="history/{tenant}/{tap}/")
    assert emitted == [txn("new"), txn("old")]


def test_merchant_id_with_underscores_is_matched_exactly(store, capsys):
    name = "optional/folders/transactions_merchant_001_delivery-2.jsonl"
    store.put(INBOUND, name, jsonl(txn("mine")))
    _, emitted = run_tap(store, capsys, merchant_id="merchant_001", input_blob=name, history_blob=None)
    assert emitted == [txn("mine")]


def test_empty_history_fails_before_emitting_new_records(store, capsys):
    history = HISTORY_PREFIX + "202611/job/transactions.jsonl"
    store.put(INBOUND, file_name(), jsonl(txn("new")))
    store.put(HISTORY, history, "")
    with pytest.raises(InvalidHistory, match="no transactions"):
        run_tap(store, capsys, input_blob=file_name(), history_blob=history)
    assert '"type": "RECORD"' not in capsys.readouterr().out
