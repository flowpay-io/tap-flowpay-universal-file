"""The shared service principal may come from environment variables instead of each tenant's settings."""

from unittest.mock import Mock

import pytest

from tap_flowpay_universal_file.tap import TapFlowpayUniversalFile
from tests.test_authentication import SP_CONFIG

VARIABLES = {
    "AZURE_TENANT_ID": "directory-from-env",
    "AZURE_CLIENT_ID": "application-from-env",
    "AZURE_CLIENT_SECRET": "secret-from-env",
}
SETTINGS = ("azure_tenant_id", "azure_client_id", "azure_client_secret")
WITHOUT_CREDENTIALS = {key: value for key, value in SP_CONFIG.items() if key not in SETTINGS}


@pytest.fixture
def credential_env(monkeypatch):
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)

    def set_variables(**variables):
        for variable, value in variables.items():
            monkeypatch.setenv(variable, value)

    return set_variables


@pytest.fixture
def credential_factory(monkeypatch):
    factory = Mock(return_value=Mock())
    monkeypatch.setattr("tap_flowpay_universal_file.storage.ClientSecretCredential", factory)
    monkeypatch.setattr("tap_flowpay_universal_file.storage.ContainerClient", Mock())
    return factory


def test_service_principal_from_environment_when_settings_have_none(credential_env, credential_factory):
    credential_env(**VARIABLES)
    TapFlowpayUniversalFile(config=WITHOUT_CREDENTIALS).store
    credential_factory.assert_called_once_with(
        tenant_id="directory-from-env", client_id="application-from-env", client_secret="secret-from-env",
    )


def test_settings_win_over_environment(credential_env, credential_factory):
    credential_env(**VARIABLES)
    TapFlowpayUniversalFile(config=SP_CONFIG).store
    credential_factory.assert_called_once_with(
        tenant_id="directory-id", client_id="application-id", client_secret="synthetic-client-secret",
    )


def test_environment_fills_only_the_missing_setting(credential_env, credential_factory):
    credential_env(AZURE_CLIENT_SECRET="secret-from-env")
    config = {key: value for key, value in SP_CONFIG.items() if key != "azure_client_secret"}
    TapFlowpayUniversalFile(config=config).store
    credential_factory.assert_called_once_with(
        tenant_id="directory-id", client_id="application-id", client_secret="secret-from-env",
    )


@pytest.mark.parametrize("missing", ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"])
def test_missing_credential_names_both_the_setting_and_the_variable(missing, credential_env):
    credential_env(**{key: value for key, value in VARIABLES.items() if key != missing})
    with pytest.raises(ValueError, match=missing) as error:
        TapFlowpayUniversalFile(config=WITHOUT_CREDENTIALS)
    assert missing.lower() in str(error.value)
    assert "secret-from-env" not in str(error.value)


def test_blank_variable_does_not_count(credential_env):
    credential_env(**{**VARIABLES, "AZURE_CLIENT_SECRET": "  "})
    with pytest.raises(ValueError, match="azure_client_secret"):
        TapFlowpayUniversalFile(config=WITHOUT_CREDENTIALS)
