"""A partner's file contract comes from configuration; GoKasa is a built-in preset."""

import json
import pathlib
import re

import pytest

from tap_flowpay_universal_file.contract import GOKASA_SETTINGS, contract_from_config
from tap_flowpay_universal_file.schema import GOKASA_SCHEMA
from tests.conftest import CSV_PARTNER

README = (pathlib.Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")


def test_gokasa_preset_is_used_when_no_contract_is_configured():
    contract = contract_from_config({"partner_code": "GoKasa"})
    assert contract.file_format == "jsonl"
    assert contract.key_properties == ("transactionId",)
    assert contract.sort_properties == ("date", "transactionId")
    assert contract.stream_name == "transactions"
    assert contract.schema == GOKASA_SCHEMA
    assert contract.input_suffix == ".jsonl"
    assert contract.history_files == ("transactions.jsonl", "transactions.csv")


def test_explicit_gokasa_settings_equal_the_preset():
    assert contract_from_config(GOKASA_SETTINGS) == contract_from_config({"partner_code": "GoKasa"})


def test_readme_documents_the_exact_gokasa_settings():
    block = re.search(r"<!-- gokasa-settings -->\s*```json\n(.*?)```", README, re.DOTALL)
    assert block, "README must contain the GoKasa settings block"
    assert json.loads(block[1]) == GOKASA_SETTINGS


def test_partner_contract_comes_from_settings():
    contract = contract_from_config(CSV_PARTNER)
    assert contract.partner_code == "ExamplePOS"
    assert contract.file_format == "csv"
    assert contract.key_properties == ("receiptId", "line")
    assert contract.sort_properties == ("soldAt",)
    assert contract.input_suffix == ".csv"
    assert (contract.csv_delimiter, contract.csv_decimal_separator) == (";", ",")


def test_schema_and_field_lists_may_be_json_text_as_host_uis_store_them():
    as_text = {
        **CSV_PARTNER,
        "schema": json.dumps(CSV_PARTNER["schema"]),
        "key_properties": json.dumps(CSV_PARTNER["key_properties"]),
        "sort_properties": json.dumps(CSV_PARTNER["sort_properties"]),
    }
    assert contract_from_config(as_text) == contract_from_config(CSV_PARTNER)


def test_sort_order_defaults_to_the_key():
    settings = {key: value for key, value in CSV_PARTNER.items() if key != "sort_properties"}
    assert contract_from_config(settings).sort_properties == ("receiptId", "line")


def test_another_partner_without_a_schema_cannot_fall_back_to_gokasa():
    with pytest.raises(ValueError, match="partner_code"):
        contract_from_config({"partner_code": "ExamplePOS"})


def test_part_of_a_contract_without_a_schema_is_rejected():
    with pytest.raises(ValueError, match="schema"):
        contract_from_config({"partner_code": "GoKasa", "file_format": "csv"})


def with_schema_properties(**properties):
    schema = {**CSV_PARTNER["schema"], "properties": {**CSV_PARTNER["schema"]["properties"], **properties}}
    return {**CSV_PARTNER, "schema": schema}


@pytest.mark.parametrize(("change", "setting"), [
    ({"partner_code": " "}, "partner_code"),
    ({"schema": "{not json"}, "schema"),
    ({"schema": {"type": "array", "items": {"type": "string"}}}, "schema"),
    ({"schema": {"type": "object", "properties": {}}}, "schema"),
    ({"schema": {"type": "object", "properties": {"a": {"type": "strin"}}}}, "schema"),
    ({"schema": {"type": "object", "properties": {"a": {"type": ["string", "integer"]}}}}, "schema"),
    ({"schema": {"type": "object", "properties": {"a": {"description": "no type"}}}}, "schema"),
    ({"key_properties": None}, "key_properties"),
    ({"key_properties": []}, "key_properties"),
    ({"key_properties": ["receiptId", "receiptId"]}, "key_properties"),
    ({"key_properties": ["missing"]}, "key_properties"),
    ({"key_properties": "receiptId"}, "key_properties"),
    ({"sort_properties": ["missing"]}, "sort_properties"),
    ({"file_format": "xlsx"}, "file_format"),
    ({"file_prefix": ""}, "file_prefix"),
    ({"file_prefix": "sales/2026"}, "file_prefix"),
    ({"stream_name": "trans/actions"}, "stream_name"),
    ({"csv_delimiter": ";;"}, "csv_delimiter"),
    ({"csv_delimiter": '"'}, "csv_delimiter"),
    ({"csv_decimal_separator": "x"}, "csv_decimal_separator"),
])
def test_invalid_contract_settings_fail_before_storage(change, setting):
    with pytest.raises(ValueError, match=setting):
        contract_from_config({**CSV_PARTNER, **change})


def test_key_fields_must_be_single_values():
    settings = with_schema_properties(items={"type": "array", "items": {"type": "string"}})
    with pytest.raises(ValueError, match="key_properties"):
        contract_from_config({**settings, "file_format": "jsonl", "key_properties": ["items"]})


def test_csv_files_cannot_hold_nested_fields():
    with pytest.raises(ValueError, match="csv"):
        contract_from_config(with_schema_properties(items={"type": "array", "items": {"type": "string"}}))


def test_filenames_follow_the_configured_prefix_and_format():
    contract = contract_from_config({**CSV_PARTNER, "file_prefix": "sales"})
    contract.validate_input_name("optional/folder/sales_M001_7.csv", "M001")
    for wrong in ("inbound/transactions_M001_7.csv", "inbound/sales_M001_7.jsonl", "inbound/sales_M002_7.csv"):
        with pytest.raises(ValueError, match="sales_<merchant_id>_<delivery_id>.csv"):
            contract.validate_input_name(wrong, "M001")
