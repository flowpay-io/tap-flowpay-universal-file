"""Validate storage credentials and job references without contacting storage."""

from __future__ import annotations

import os
import typing as t
from urllib.parse import urlsplit

from tap_flowpay_universal_file.contract import contract_from_config

JOB_VARIABLES = {"{tenant}": "TENANT", "{tap}": "TAP"}


def validate_blob_name(name: str, setting: str) -> None:
    """Accept a relative, unencoded blob name, never a URL or credential-bearing URI."""
    if (
        not name
        or any(char in name for char in "\\:?#%")
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        # Do not include the value: a caller may have accidentally supplied a SAS URL.
        raise ValueError(f"{setting} must be a relative blob name without URL, query or traversal segments")


def history_prefix(config: t.Mapping) -> str:
    """Resolve the job's history folder and enforce a trailing directory boundary."""
    prefix = config["history_prefix"]
    for placeholder, variable in JOB_VARIABLES.items():
        if placeholder in prefix:
            value = os.environ.get(variable)
            if not value:
                raise ValueError(f"history_prefix uses {placeholder}, but {variable} is not set in the job environment")
            validate_blob_name(value, variable)
            if "/" in value:
                raise ValueError(f"{variable} must be a single path component")
            prefix = prefix.replace(placeholder, value)
    validate_blob_name(prefix.rstrip("/"), "history_prefix")
    if "{" in prefix or "}" in prefix:
        raise ValueError("history_prefix contains an unsupported placeholder")
    return prefix.rstrip("/") + "/"


def validate_settings(config: t.Mapping) -> None:
    """Check conditional configuration requirements with secret-free errors."""
    method = config["auth_method"]
    if method == "service_principal":
        fields = ("azure_tenant_id", "azure_client_id", "azure_client_secret")
    elif method == "sas":
        fields = ("inbound_sas_token", "history_sas_token")
    else:
        raise ValueError("auth_method must be service_principal or sas")
    for field in fields:
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise ValueError(f"{field} is required for auth_method={method}")

    endpoint = urlsplit(config["account_url"])
    if (
        endpoint.scheme != "https" or not endpoint.hostname
        or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
        or endpoint.path not in ("", "/")
    ):
        raise ValueError("account_url must be an HTTPS account endpoint without credentials, query or path")
    for field in ("inbound_container", "history_container", "merchant_id"):
        validate_blob_name(config[field], field)
        if "/" in config[field]:
            raise ValueError(f"{field} must be a single path component")

    if (
        method == "sas" and config["inbound_container"] == config["history_container"]
        and config["inbound_sas_token"] != config["history_sas_token"]
    ):
        raise ValueError("Use the same SAS token when input and history use the same container")
    if config["archive_processed"]:
        raise ValueError("archive_processed=true is no longer supported; the tap leaves source files in place")

    contract = contract_from_config(config)
    if config["require_input_blob"] and "input_blob" not in config:
        raise ValueError("input_blob is required; prefix scanning is disabled by require_input_blob")
    if "history_blob" in config and "input_blob" not in config:
        raise ValueError("input_blob is required when history_blob is supplied; omit both for legacy scanning")
    if "input_blob" in config:
        validate_blob_name(config["input_blob"], "input_blob")
        contract.validate_input_name(config["input_blob"], config["merchant_id"])
        if "history_blob" not in config:
            raise ValueError("Exact-file jobs require history_blob: a previous successful output, or null for the first run")
    prefix = history_prefix(config)
    if config.get("history_blob") is not None:
        previous = config["history_blob"]
        validate_blob_name(previous, "history_blob")
        if not previous.startswith(prefix):
            raise ValueError("history_blob must be inside this connection's history_prefix")
        if previous.rsplit("/", 1)[-1] not in contract.history_files:
            raise ValueError(f"history_blob must name {' or '.join(contract.history_files)}")
