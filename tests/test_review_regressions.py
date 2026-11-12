"""Invalid partner data must fail early, and history replay must preserve exact keys."""

import copy
import pytest

from tap_flowpay_universal_file.contract import contract_from_config
from tap_flowpay_universal_file.history import InvalidHistory
from tap_flowpay_universal_file.records import InvalidDelivery
from tests.test_tap import INBOUND, file_name, run_tap


SETTINGS = {
    "partner_code": "ExamplePOS",
    "key_properties": ["id"],
    "schema": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "note": {"type": ["string", "null"]}},
    },
}


def settings(**changes):
    return {**copy.deepcopy(SETTINGS), **changes}


def numeric_contract():
    return contract_from_config(settings(file_format="csv", schema={
        "type": "object", "properties": {"id": {"type": "integer"}, "amount": {"type": "number"}},
    }))


@pytest.mark.parametrize("text", [
    "9007199254740993", "9007199254740993.0", "9.007199254740993e15", "+9007199254740993",
])
def test_csv_history_preserves_large_integer_keys_during_merge(text):
    contract = numeric_contract()
    history = contract.read_history(f"id,amount\n{text},1\n9007199254740992,2\n".encode(), "history.csv")
    assert history == [{"id": 9007199254740993, "amount": 1}, {"id": 9007199254740992, "amount": 2}]
    incoming = contract.read_delivery(b"id,amount\n9007199254740993,3\n", "delivery.csv")

    merged, _ = contract.merge(history, incoming.records)

    assert merged == [{"id": 9007199254740992, "amount": 2}, {"id": 9007199254740993, "amount": 3}]
    assert all(type(row["id"]) is int for row in history)


@pytest.mark.parametrize("text", ["9007199254740993.1", "1.0000000000000000001"])
def test_fractional_integer_history_cannot_be_rounded_into_a_valid_key(text):
    with pytest.raises(InvalidHistory, match="history.csv line 2"):
        numeric_contract().read_history(f"id,amount\n{text},1\n".encode(), "history.csv")


@pytest.mark.parametrize("value", ["1e309", "-1e309", "NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("history", [False, True])
def test_non_finite_numbers_fail_in_delivery_and_history(value, history):
    contract = numeric_contract()
    read = contract.read_history if history else contract.read_delivery
    error = InvalidHistory if history else InvalidDelivery
    with pytest.raises(error, match="file.jsonl line 1"):
        read(('{"id":1,"amount":' + value + '}\n').encode(), "file.jsonl")


@pytest.mark.parametrize("history", [False, True])
def test_csv_overflow_is_rejected_before_returning_records(history):
    contract = numeric_contract()
    read = contract.read_history if history else contract.read_delivery
    with pytest.raises(InvalidHistory if history else InvalidDelivery, match="file.csv line 2"):
        read(b"id,amount\n1,1e309\n", "file.csv")


@pytest.mark.parametrize(("content", "line"), [
    (b'id,note\na,"unfinished\n', 2),
    (b'id,note\na,"closed"junk\n', 2),
    (b'"id,note\n', 1),
])
@pytest.mark.parametrize("history", [False, True])
def test_malformed_csv_is_rejected_with_source_and_line(content, line, history):
    contract = contract_from_config(settings(file_format="csv"))
    read = contract.read_history if history else contract.read_delivery
    with pytest.raises(InvalidHistory if history else InvalidDelivery, match=f"file.csv line {line}: not valid CSV"):
        read(content, "file.csv")


def test_valid_multiline_and_escaped_csv_fields_are_preserved():
    contract = contract_from_config(settings(file_format="csv"))
    content = b'id,note\na,"line 1\nline 2, with ""quotes"""\n'
    expected = [{"id": "a", "note": 'line 1\nline 2, with "quotes"'}]
    assert contract.read_delivery(content, "file.csv").records == expected
    assert contract.read_history(content, "file.csv") == expected


def test_nullable_root_is_a_configuration_error():
    config = settings()
    config["schema"]["type"] = ["object", "null"]
    with pytest.raises(ValueError, match="schema"):
        contract_from_config(config)


@pytest.mark.parametrize("kind", ["string", "integer", "number", "boolean"])
def test_nullable_key_is_a_configuration_error(kind):
    config = settings()
    config["schema"]["properties"]["id"]["type"] = [kind, "null"]
    with pytest.raises(ValueError, match="key_properties"):
        contract_from_config(config)


def test_nullable_sort_fields_remain_supported():
    contract = contract_from_config(settings(sort_properties=["note"]))
    delivery = contract.read_delivery(b'{"id":"a","note":null}\n{"id":"b","note":"text"}\n', "file.jsonl")
    records, _ = contract.merge(delivery.records)
    assert records == [{"id": "b", "note": "text"}, {"id": "a"}]


def test_empty_key_is_rejected_even_with_explicit_zero_minimum_length():
    config = settings()
    config["schema"]["properties"]["id"]["minLength"] = 0
    with pytest.raises(InvalidDelivery, match="file.jsonl line 1"):
        contract_from_config(config).read_delivery(b'{"id":""}\n', "file.jsonl")


def test_stricter_key_length_is_preserved():
    config = settings()
    config["schema"]["properties"]["id"]["minLength"] = 3
    contract = contract_from_config(config)
    with pytest.raises(InvalidDelivery):
        contract.read_delivery(b'{"id":"a"}\n', "file.jsonl")
    assert contract.read_delivery(b'{"id":"abc"}\n', "file.jsonl").records == [{"id": "abc"}]


def test_objects_without_declared_properties_drop_and_count_nested_unknown_fields():
    config = settings()
    config["schema"]["properties"].update({
        "details": {"type": "object"},
        "items": {"type": "array", "items": {"type": "object"}},
    })
    contract = contract_from_config(config)
    content = b'{"id":"a","details":{"extra":"private"},"items":[{"extra":"private"}]}\n'
    delivery = contract.read_delivery(content, "file.jsonl")
    expected = [{"id": "a", "details": {}, "items": [{}]}]
    assert delivery.records == expected
    assert delivery.ignored == {"details.extra": 1, "items[].extra": 1}
    assert contract.read_history(content, "history.jsonl") == expected


@pytest.mark.parametrize("items", [None, True, [{"type": "object"}]])
def test_arrays_require_a_single_explicit_item_schema(items):
    config = settings()
    node = {"type": "array"}
    if items is not None:
        node["items"] = items
    config["schema"]["properties"]["items"] = node
    with pytest.raises(ValueError, match="schema"):
        contract_from_config(config)


def test_bad_later_csv_row_cannot_emit_a_partial_history(store, capsys):
    config = settings(file_format="csv")
    name = file_name().replace(".jsonl", ".csv")
    store.put(INBOUND, name, 'id,note\ngood,ok\nbad,"unfinished\n')
    with pytest.raises(InvalidDelivery, match="not valid CSV"):
        run_tap(store, capsys, **config, input_blob=name, history_blob=None)
    assert '"type": "RECORD"' not in capsys.readouterr().out


def test_nested_non_finite_number_is_rejected():
    config = settings()
    config["schema"]["properties"]["details"] = {
        "type": "object", "properties": {"amount": {"type": "number"}},
    }
    with pytest.raises(InvalidDelivery, match="file.jsonl line 1"):
        contract_from_config(config).read_delivery(b'{"id":"a","details":{"amount":1e309}}\n', "file.jsonl")
