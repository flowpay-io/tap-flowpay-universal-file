"""The `transactions` stream: one GoKasa transaction, exactly as GoKasa sends it.

The same schema is the contract GoKasa's files are checked against. Downstream
consumers interpret the fields; the tap does not.
"""

from __future__ import annotations

import copy
import typing as t

from jsonschema import Draft7Validator, FormatChecker
from singer_sdk import typing as th

PAYMENT = th.ObjectType(
    th.Property("paymentMethod", th.StringType, description="CASH, CREDIT_CARD or BANK_TRANSFER"),
    th.Property("amount", th.IntegerType, required=True, description="Haléř; negative for a refund"),
    th.Property("currency", th.StringType, required=True),
    th.Property("acceptedAmount", th.IntegerType),
    th.Property("acceptedCurrency", th.StringType),
)

SCHEMA = th.PropertiesList(
    th.Property("transactionId", th.StringType, required=True, description="Merge key"),
    th.Property("payments", th.ArrayType(PAYMENT), required=True, description="More than one for a split bill"),
    th.Property("date", th.DateType, required=True),
    th.Property("checkoutId", th.StringType),
    th.Property("cancel", th.IntegerType, description="On a sale: the checkoutId of the refund that cancels it"),
).to_dict()

FIELDS = frozenset(SCHEMA["properties"])


def _contract(schema: dict) -> dict:
    """The stream schema, made strict for checking files.

    The SDK silently drops fields that are not in the schema, so an unknown field
    fails the job instead: the schema has to be extended before the data is lost.
    """
    contract = copy.deepcopy(schema)
    contract["additionalProperties"] = False
    contract["properties"]["transactionId"]["minLength"] = 1
    contract["properties"]["payments"]["items"]["additionalProperties"] = False
    return contract


_VALIDATOR = Draft7Validator(_contract(SCHEMA), format_checker=FormatChecker())


def problems(record: t.Any) -> list[str]:
    """What breaks the contract in one record, as 'path: message'. Empty when it fits."""
    found = []
    for error in sorted(_VALIDATOR.iter_errors(record), key=lambda e: e.json_path):
        path = error.json_path[2:]  # "$.payments[0].amount" -> "payments[0].amount"
        found.append(f"{path}: {error.message}" if path else error.message)
    return found
