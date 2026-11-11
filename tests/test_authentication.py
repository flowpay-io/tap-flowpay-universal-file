"""Credential selection and Azure client wiring without contacting Azure."""

from unittest.mock import Mock

import pytest

from tap_gokasa.tap import TapGoKasa
from tests.test_tap import CONFIG, HISTORY, INBOUND

SP_CONFIG = {
    **{key: value for key, value in CONFIG.items() if not key.endswith("sas_token")},
    "auth_method": "service_principal",
    "azure_tenant_id": "directory-id",
    "azure_client_id": "application-id",
    "azure_client_secret": "synthetic-client-secret",
}


def test_service_principal_is_shared_by_both_container_clients(monkeypatch):
    credential = Mock()
    credential_factory = Mock(return_value=credential)
    client_factory = Mock()
    monkeypatch.setattr("tap_gokasa.storage.ClientSecretCredential", credential_factory)
    monkeypatch.setattr("tap_gokasa.storage.ContainerClient", client_factory)
    tap = TapGoKasa(config=SP_CONFIG)

    store = tap.store

    credential_factory.assert_called_once_with(
        tenant_id="directory-id", client_id="application-id", client_secret="synthetic-client-secret",
    )
    assert {call.args[1] for call in client_factory.call_args_list} == {INBOUND, HISTORY}
    assert all(call.kwargs["credential"] is credential for call in client_factory.call_args_list)
    assert not credential.get_token.called  # The Azure SDK obtains tokens on demand.
    assert tap.store is store


def test_legacy_configuration_keeps_separate_container_sas_credentials(monkeypatch):
    client_factory = Mock()
    monkeypatch.setattr("tap_gokasa.storage.ContainerClient", client_factory)
    TapGoKasa(config=CONFIG).store
    assert {call.args[1]: call.kwargs["credential"] for call in client_factory.call_args_list} == {
        INBOUND: "sv=inbound", HISTORY: "sv=history",
    }


@pytest.mark.parametrize("missing", ["azure_tenant_id", "azure_client_id", "azure_client_secret"])
def test_service_principal_requires_all_three_fields_without_printing_secrets(missing):
    config = {key: value for key, value in SP_CONFIG.items() if key != missing}
    with pytest.raises(ValueError, match=missing) as error:
        TapGoKasa(config=config)
    assert "synthetic-client-secret" not in str(error.value)


@pytest.mark.parametrize("missing", ["inbound_sas_token", "history_sas_token"])
def test_sas_mode_requires_both_credentials(missing):
    with pytest.raises(ValueError, match=missing):
        TapGoKasa(config={key: value for key, value in CONFIG.items() if key != missing})


def test_authentication_mode_is_explicit_and_rejects_unknown_methods():
    with pytest.raises(ValueError, match="auth_method"):
        TapGoKasa(config={**CONFIG, "auth_method": "unknown"})


def test_service_principal_does_not_fall_back_to_stale_sas_tokens(monkeypatch):
    client_factory = Mock()
    credential = Mock()
    monkeypatch.setattr("tap_gokasa.storage.ContainerClient", client_factory)
    monkeypatch.setattr("tap_gokasa.storage.ClientSecretCredential", Mock(return_value=credential))
    TapGoKasa(config={**CONFIG, **SP_CONFIG}).store
    assert all(call.kwargs["credential"] is credential for call in client_factory.call_args_list)


def test_different_sas_tokens_for_the_same_container_are_rejected():
    with pytest.raises(ValueError, match="same container"):
        TapGoKasa(config={**CONFIG, "history_container": INBOUND})


def test_secrets_are_marked_in_discovery_configuration():
    properties = TapGoKasa.config_jsonschema["properties"]
    for key in ("inbound_sas_token", "history_sas_token", "azure_client_secret"):
        assert properties[key]["secret"] is True
