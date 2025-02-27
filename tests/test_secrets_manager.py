from datetime import date, timedelta

import boto3
import pytest
from moto import mock_secretsmanager
from prefect import flow

from prefect_aws.secrets_manager import (
    create_secret,
    delete_secret,
    read_secret,
    update_secret,
)


@pytest.fixture
def secretsmanager_client():
    with mock_secretsmanager():
        yield boto3.client("secretsmanager", "us-east-1")


@pytest.fixture(
    params=[
        dict(Name="secret_string_no_version", SecretString="1"),
        dict(
            Name="secret_string_with_version_id", SecretString="2", should_version=True
        ),
        dict(Name="secret_binary_no_version", SecretBinary=b"3"),
        dict(
            Name="secret_binary_with_version_id", SecretBinary=b"4", should_version=True
        ),
    ]
)
def secret_under_test(secretsmanager_client, request):
    should_version = request.param.pop("should_version", False)
    secretsmanager_client.create_secret(**request.param)

    update_result = None
    if should_version:
        if "SecretString" in request.param:
            request.param["SecretString"] = request.param["SecretString"] + "-versioned"
        elif "SecretBinary" in request.param:
            request.param["SecretBinary"] = (
                request.param["SecretBinary"] + b"-versioned"
            )
        update_secret_kwargs = request.param.copy()
        update_secret_kwargs["SecretId"] = update_secret_kwargs.pop("Name")
        update_result = secretsmanager_client.update_secret(**update_secret_kwargs)

    return dict(
        secret_name=request.param.get("Name"),
        version_id=update_result.get("VersionId") if update_result else None,
        expected_value=request.param.get("SecretString")
        or request.param.get("SecretBinary"),
    )


async def test_read_secret(secret_under_test, aws_credentials):
    expected_value = secret_under_test.pop("expected_value")

    @flow
    async def test_flow():
        return await read_secret(
            aws_credentials=aws_credentials,
            **secret_under_test,
        )

    assert (await test_flow()).result().result() == expected_value


async def test_update_secret(secret_under_test, aws_credentials, secretsmanager_client):
    current_secret_value = secret_under_test["expected_value"]
    new_secret_value = (
        current_secret_value + "2"
        if isinstance(current_secret_value, str)
        else current_secret_value + b"2"
    )

    @flow
    async def test_flow():
        return await update_secret(
            aws_credentials=aws_credentials,
            secret_name=secret_under_test["secret_name"],
            secret_value=new_secret_value,
        )

    flow_state = await test_flow()
    assert flow_state.result().result().get("Name") == secret_under_test["secret_name"]

    updated_secret = secretsmanager_client.get_secret_value(
        SecretId=secret_under_test["secret_name"]
    )
    assert (
        updated_secret.get("SecretString") == new_secret_value
        or updated_secret.get("SecretBinary") == new_secret_value
    )


@pytest.mark.parametrize(
    ["secret_name", "secret_value"], [["string_secret", "42"], ["binary_secret", b"42"]]
)
async def test_create_secret(
    aws_credentials, secret_name, secret_value, secretsmanager_client
):
    @flow
    async def test_flow():
        return await create_secret(
            secret_name=secret_name,
            secret_value=secret_value,
            aws_credentials=aws_credentials,
        )

    flow_state = await test_flow()
    assert flow_state.result().result().get("Name") == secret_name

    updated_secret = secretsmanager_client.get_secret_value(SecretId=secret_name)
    assert (
        updated_secret.get("SecretString") == secret_value
        or updated_secret.get("SecretBinary") == secret_value
    )


@pytest.mark.parametrize(
    ["recovery_window_in_days", "force_delete_without_recovery"],
    [
        [30, False],
        [90, False],
        [7, False],
        [6, False],
        [10, False],
        [15, True],
        [31, True],
    ],
)
async def test_delete_secret(
    aws_credentials,
    secret_under_test,
    recovery_window_in_days,
    force_delete_without_recovery,
):
    @flow
    async def test_flow():
        return await delete_secret(
            secret_name=secret_under_test["secret_name"],
            aws_credentials=aws_credentials,
            recovery_window_in_days=recovery_window_in_days,
            force_delete_without_recovery=force_delete_without_recovery,
        )

    flow_state = await test_flow()
    if not force_delete_without_recovery and not 7 <= recovery_window_in_days <= 30:
        with pytest.raises(ValueError):
            result = flow_state.result().result()

    else:
        result = flow_state.result().result()
        assert result.get("Name") == secret_under_test["secret_name"]
        deletion_date = result.get("DeletionDate")
        if not force_delete_without_recovery:
            assert deletion_date.date() == (
                date.today() + timedelta(days=recovery_window_in_days)
            )
        else:
            assert deletion_date.date() == date.today()
