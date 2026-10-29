from unittest.mock import Mock

import pytest

import up


@pytest.mark.parametrize("missing", ["UP_ACCOUNT_ID", "UP_ACCESS_TOKEN"])
def test_missing_settings_stop_before_request(missing, monkeypatch, capsys):
    monkeypatch.setenv("UP_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("UP_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv(missing)
    get = Mock(side_effect=AssertionError("unexpected API request"))
    monkeypatch.setattr(up.requests, "get", get)
    with pytest.raises(SystemExit) as caught:
        up.convert_json_to_csv("2026-10-01T00:00:00+10:00")
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert missing in output.err
    assert output.out == ""
    get.assert_not_called()


def test_up_uses_configured_account_and_token(monkeypatch, capsys):
    monkeypatch.setenv("UP_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("UP_ACCESS_TOKEN", "test-token")
    response = Mock(status_code=200)
    response.json.return_value = {"data": [], "links": {"next": None}}
    get = Mock(return_value=response)
    monkeypatch.setattr(up.requests, "get", get)
    since = "2026-10-01T00:00:00+10:00"
    up.convert_json_to_csv(since)
    get.assert_called_once_with(
        "https://api.up.com.au/api/v1/accounts/test-account/transactions",
        headers={"Authorization": "Bearer test-token", "Accept": "application/json"},
        params={"filter[since]": since, "page[size]": 100},
    )
    assert capsys.readouterr().out == ""
