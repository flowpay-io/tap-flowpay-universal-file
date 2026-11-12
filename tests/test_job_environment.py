"""Per-job parameters given as environment variables, the way the orchestrator starts jobs."""

from unittest.mock import Mock

import pytest
from singer_sdk.exceptions import ConfigValidationError

from tap_flowpay_universal_file.tap import TapFlowpayUniversalFile
from tests.conftest import jsonl, txn
from tests.test_tap import CONFIG, HISTORY, HISTORY_PREFIX, INBOUND, file_name, run_tap

# A saved event-driven configuration: no merchant or file; the job supplies them.
SAVED = {key: value for key, value in CONFIG.items() if key != "merchant_id"}


@pytest.fixture
def job_env(monkeypatch):
    for variable in ("MERCHANT_ID", "BLOB_PATH", "HISTORY_BLOB"):
        monkeypatch.delenv(variable, raising=False)

    def set_job(**variables):
        for variable, value in variables.items():
            monkeypatch.setenv(variable, value)

    return set_job


def no_storage(store, monkeypatch):
    monkeypatch.setattr(store, "list_blobs", Mock(side_effect=AssertionError("must not list storage")))


@pytest.fixture(autouse=True)
def saved_config(monkeypatch):
    """run_tap merges into CONFIG; drop merchant_id so the job environment must supply it."""
    monkeypatch.setattr("tests.test_tap.CONFIG", SAVED)


@pytest.mark.parametrize("first_file", ["", "null"])
def test_first_file_from_job_environment(store, capsys, monkeypatch, job_env, first_file):
    no_storage(store, monkeypatch)
    store.put(INBOUND, file_name(), jsonl(txn("t1")))
    job_env(MERCHANT_ID="M001", BLOB_PATH=file_name(), HISTORY_BLOB=first_file)

    _, emitted = run_tap(store, capsys, require_input_blob=True)

    assert emitted == [txn("t1")]


def test_next_file_merges_into_the_history_from_job_environment(store, capsys, monkeypatch, job_env):
    no_storage(store, monkeypatch)
    history = HISTORY_PREFIX + "202611/job-1/transactions.jsonl"
    store.put(HISTORY, history, jsonl(txn("old", date="2026-08-01")))
    store.put(INBOUND, file_name(2), jsonl(txn("new", date="2026-09-01")))
    job_env(MERCHANT_ID="M001", BLOB_PATH=file_name(2), HISTORY_BLOB=history)

    _, emitted = run_tap(store, capsys, require_input_blob=True)

    assert [r["transactionId"] for r in emitted] == ["old", "new"]


def test_job_environment_wins_over_saved_file_references(store, capsys, monkeypatch, job_env):
    no_storage(store, monkeypatch)
    saved_history = HISTORY_PREFIX + "202611/saved/transactions.jsonl"
    store.put(HISTORY, saved_history, jsonl(txn("saved-history")))
    store.put(INBOUND, file_name(1), jsonl(txn("saved-input")))
    store.put(INBOUND, file_name(2), jsonl(txn("uploaded")))
    job_env(MERCHANT_ID="M001", BLOB_PATH=file_name(2), HISTORY_BLOB="")

    _, emitted = run_tap(store, capsys, merchant_id="M001", input_blob=file_name(1), history_blob=saved_history)

    assert emitted == [txn("uploaded")]


@pytest.mark.parametrize("variables", [
    {"MERCHANT_ID": "M001", "BLOB_PATH": "inbound/M001/transactions_M001_1.jsonl"},
    {"MERCHANT_ID": "M001", "HISTORY_BLOB": ""},
])
def test_file_and_history_must_come_from_the_same_job(variables, store, capsys, monkeypatch, job_env):
    # Never combine a per-job file with a history saved for an earlier manual run.
    no_storage(store, monkeypatch)
    job_env(**variables)
    with pytest.raises(ValueError, match="BLOB_PATH and HISTORY_BLOB"):
        run_tap(store, capsys, input_blob=file_name(), history_blob=HISTORY_PREFIX + "202611/s/transactions.jsonl")


@pytest.mark.parametrize(("variables", "setting"), [
    ({"MERCHANT_ID": "M002"}, "input_blob"),
    ({"BLOB_PATH": "https://example.blob.core.windows.net/x/transactions_M001_1.jsonl?sig=do-not-print"}, "input_blob"),
    ({"HISTORY_BLOB": "history/other-connection/202611/job/transactions.jsonl"}, "history_blob"),
    ({"MERCHANT_ID": ""}, "merchant_id"),
])
def test_invalid_job_environment_fails_before_storage(variables, setting, store, capsys, monkeypatch, job_env):
    no_storage(store, monkeypatch)
    monkeypatch.setattr(store, "read", Mock(side_effect=AssertionError("must not read storage")))
    job_env(**{"MERCHANT_ID": "M001", "BLOB_PATH": file_name(), "HISTORY_BLOB": "", **variables})
    with pytest.raises(ValueError, match=setting) as error:
        run_tap(store, capsys, require_input_blob=True)
    assert "do-not-print" not in str(error.value)


def test_without_job_environment_saved_settings_still_work(store, capsys, monkeypatch, job_env):
    no_storage(store, monkeypatch)
    store.put(INBOUND, file_name(), jsonl(txn("manual")))

    _, emitted = run_tap(store, capsys, merchant_id="M001", input_blob=file_name(), history_blob=None)

    assert emitted == [txn("manual")]


def test_event_configuration_without_job_parameters_still_fails(job_env):
    with pytest.raises((ConfigValidationError, ValueError), match="merchant_id|input_blob"):
        TapFlowpayUniversalFile(config={**SAVED, "require_input_blob": True})
