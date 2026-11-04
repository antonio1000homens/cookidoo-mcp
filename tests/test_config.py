"""SSM secrets are loaded lazily and never supplied as CloudFormation values."""
from unittest.mock import MagicMock

import boto3
import pytest

import src.config as config


def reset(monkeypatch):
    monkeypatch.setattr(config, "_bootstrapped", False)
    for key in (
        "COOKIDOO_EMAIL", "COOKIDOO_PASSWORD",
        "COOKIDOO_EMAIL_PARAM", "COOKIDOO_PASSWORD_PARAM",
        "COOKIDOO_ALLOW_LOCAL_ENV",
    ):
        monkeypatch.delenv(key, raising=False)


def test_missing_ssm_configuration_denied(monkeypatch):
    reset(monkeypatch)
    with pytest.raises(RuntimeError, match="SSM parameters"):
        config.ensure_credentials()


def test_ssm_values_loaded_once(monkeypatch):
    reset(monkeypatch)
    monkeypatch.setenv("COOKIDOO_EMAIL_PARAM", "/cookidoo-mcp/email")
    monkeypatch.setenv("COOKIDOO_PASSWORD_PARAM", "/cookidoo-mcp/password")
    client = MagicMock()
    client.get_parameters.return_value = {
        "Parameters": [
            {"Name": "/cookidoo-mcp/email", "Value": "test@example.invalid"},
            {"Name": "/cookidoo-mcp/password", "Value": "not-a-real-password"},
        ],
        "InvalidParameters": [],
    }
    monkeypatch.setattr(boto3, "client", lambda service: client)
    config.ensure_credentials()
    assert config.os.environ["COOKIDOO_EMAIL"] == "test@example.invalid"
    config.ensure_credentials()
    client.get_parameters.assert_called_once_with(
        Names=["/cookidoo-mcp/email", "/cookidoo-mcp/password"],
        WithDecryption=True,
    )


def test_missing_parameter_rejects_without_leaking(monkeypatch):
    reset(monkeypatch)
    monkeypatch.setenv("COOKIDOO_EMAIL_PARAM", "/cookidoo-mcp/email")
    monkeypatch.setenv("COOKIDOO_PASSWORD_PARAM", "/cookidoo-mcp/password")
    client = MagicMock()
    client.get_parameters.return_value = {"Parameters": [], "InvalidParameters": ["/cookidoo-mcp/password"]}
    monkeypatch.setattr(boto3, "client", lambda service: client)
    with pytest.raises(RuntimeError, match="bootstrap failed") as error:
        config.ensure_credentials()
    assert "/cookidoo-mcp/password" not in str(error.value)


def test_local_credentials_require_explicit_opt_in(monkeypatch):
    reset(monkeypatch)
    monkeypatch.setenv("COOKIDOO_EMAIL", "local@example.invalid")
    monkeypatch.setenv("COOKIDOO_PASSWORD", "not-a-real-password")
    with pytest.raises(RuntimeError):
        config.ensure_credentials()
    monkeypatch.setenv("COOKIDOO_ALLOW_LOCAL_ENV", "1")
    config.ensure_credentials()
