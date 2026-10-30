import csv
import io
import json
import subprocess
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from bs4 import BeautifulSoup

import danamon as d


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SINCE = date(2024, 10, 3)
RATE = Decimal("12636")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("tests must not contact live services")
    monkeypatch.setattr(requests.Session, "request", blocked)


def email(name="qris"):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def change_field(message, label, value):
    soup = BeautifulSoup(message["body"], "html.parser")
    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) == 2 and cells[0].get_text(" ", strip=True).strip() == label:
            if value is None:
                row.decompose()
            else:
                cells[1].clear()
                cells[1].append(value)
            break
    else:
        raise AssertionError("fixture label not found: " + label)
    message["body"] = str(soup)
    return message


def response(data, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    return result


def message_data(message):
    return {"message_id": message["id"], "inbox_id": "inbox", "subject": message["subject"],
            "html": message.get("body"), "extracted_html": ""}


def page(items, token=None):
    return response({"messages": [message_data(item) for item in items],
                     "count": len(items), "next_page_token": token})


@pytest.mark.parametrize("name,when,amount,description,reference", [
    ("qris", "2024-10-11T14:35:00", "-123450", "Example Cafe", "1000000000000001"),
    ("bank_transfer", "2024-10-17T11:20:00", "-275000", "Example Recipient - Example invoice", "TRANSFER-EXAMPLE-001"),
    ("gopay_topup", "2024-10-03T09:10:00", "-81500", "GoPay top up (incl. 1,500 fee)", "GOPAY-EXAMPLE-001"),
])
def test_fixture(name, when, amount, description, reference):
    transaction = d.parse_email(email(name))
    assert transaction.occurred_at == datetime.fromisoformat(when)
    assert transaction.amount == Decimal(amount)
    assert transaction.description == description
    assert transaction.reference == reference


def test_expected_timestamp_tsv_and_inclusive_bank_date_filter():
    messages = d.load_fixtures(FIXTURES)
    batch = d.build_batch(messages, SINCE)
    assert batch.issues == []
    result = d.to_tsv(batch.transactions, RATE)
    assert result == (FIXTURES / "expected.tsv").read_text()
    rows = list(csv.reader(io.StringIO(result), delimiter="\t"))
    assert all(len(row) == 5 and row[3] == "" for row in rows)
    assert all(line.count("\t") == 4 for line in result.splitlines())
    batch = d.build_batch(messages, date(2024, 10, 11))
    assert [tx.kind for tx in batch.transactions] == ["qris", "transfer"]
    assert batch.excluded == 1  # GoPay received on the 24th, but transacted on the 3rd.


def test_order_by_bank_time_and_stable_ties():
    first = email()
    second = change_field(email(), "Reference No", "other-reference")
    second["createdAt"] = "2000-01-01T00:00:00Z"
    earlier = change_field(email(), "Reference No", "earlier-reference")
    change_field(earlier, "Payment Date", "11 Oktober 2024 13:00")
    batch = d.build_batch([first, second, earlier], SINCE)
    assert [tx.reference for tx in batch.transactions] == ["earlier-reference", "1000000000000001", "other-reference"]


def test_duplicate_forward_and_no_persistent_state():
    original, forwarded = email(), email()
    forwarded.update(id="another-email", subject="Fwd: Fwd: " + forwarded["subject"], read=False)
    first = d.build_batch([original, forwarded], SINCE)
    second = d.build_batch([original, forwarded], SINCE)
    assert first == second
    assert len(first.transactions) == 1
    assert first.duplicates == 1


@pytest.mark.parametrize("label,value", [("Total", "Rp123.451,00"), ("Payment Date", "11 Oktober 2024 14:36"),
                                         ("To Merchant", "Different merchant")])
def test_conflicting_references_exclude_every_copy(label, value):
    batch = d.build_batch([email(), change_field(email(), label, value), email()], SINCE)
    assert not batch.transactions
    assert any("conflicting" in issue for issue in batch.issues)


@pytest.mark.parametrize("label,value", [("Amount", None), ("Total", ""), ("Total", "Rp123,45.00"),
    ("Total", "Rp0,00"), ("Total", "Rp1,00"), ("Payment Date", None),
    ("Payment Date", "31 February 2024, 07:00"), ("Reference No", None),
    ("Reference No", "=1+1"), ("Status", "Failed"), ("Status", None), ("To Merchant", "")])
def test_bad_transaction_flagged(label, value):
    batch = d.build_batch([change_field(email(), label, value)], SINCE)
    assert not batch.transactions
    assert len(batch.issues) == 1


@pytest.mark.parametrize("value", [{}, [], {"subject": "Incoming transfer"}, {"subject": None},
                                    {"subject": "QRIS Payment Successful (Do Not Reply)", "body": None}])
def test_unknown_and_missing_bodies_flagged(value):
    assert d.build_batch([value], SINCE).issues


def test_multiple_notifications_in_one_forward_rejected():
    message = email()
    message["body"] += email()["body"]
    with pytest.raises(d.ImportProblem, match="repeated"):
        d.parse_email(message)


def test_optional_transfer_notes_and_qris_total():
    assert d.parse_email(change_field(email("bank_transfer"), "Notes", None)).description == "Example Recipient"
    assert d.parse_email(change_field(email(), "Total", None)).amount == Decimal("-123450")


@pytest.mark.parametrize("label,value", [("Admin Fee", None), ("Total Amount", "Rp80.000,00"),
                                         ("Total Amount", None), ("Biller", "Other wallet")])
def test_gopay_requires_consistent_confirmed_fee(label, value):
    with pytest.raises(d.ImportProblem):
        d.parse_email(change_field(email("gopay_topup"), label, value))


def test_zero_fee_gopay():
    message = change_field(email("gopay_topup"), "Admin Fee", "Rp0,00")
    change_field(message, "Total Amount", "Rp80.000,00")
    tx = d.parse_email(message)
    assert tx.amount == -80000
    assert tx.description == "GoPay top up"


@pytest.mark.parametrize("value,expected", [("Rp123.450,00", "123450"), ("Rp 1.234,56", "1234.56"),
                                           ("Rp0,50", "0.50"), ("Rp81500", "81500")])
def test_exact_rupiah_parsing(value, expected):
    assert d.parse_amount(value) == Decimal(expected)


@pytest.mark.parametrize("value", ["Rp1,234.56", "Rp12.34,00", "Rp-10,00", "NaN", "Rp1,2", "123450"])
def test_reject_ambiguous_amounts(value):
    with pytest.raises(d.ImportProblem):
        d.parse_amount(value)


@pytest.mark.parametrize("description", ["=IMPORTXML(\"bad\")", " \t+1", "\r\n-1", "@SUM(A:A)", "\ufeff=1", "\u200b=1"])
def test_formula_injection(description):
    tx = d.parse_email(change_field(email(), "To Merchant", description))
    result = d.to_tsv([tx], RATE)
    row = next(csv.reader(io.StringIO(result), delimiter="\t"))
    assert row[4].startswith("'")
    assert len(result.splitlines()) == 1
    assert result.count("\t") == 4


def test_cell_injection_quotes_and_unicode():
    tx = d.parse_email(change_field(email(), "To Merchant", 'Café "A"\tB\nC'))
    result = d.to_tsv([tx], Decimal("12345.67"))
    row = next(csv.reader(io.StringIO(result), delimiter="\t"))
    assert row[4] == 'Café "A" B C'
    assert row[2] == "-10.00"
    assert result.count("\t") == 4
    assert d.to_tsv([replace(tx, amount=Decimal("-100.50"))], RATE).split("\t")[1] == "-100.5"


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1e99", "=1+1", "12,636"])
def test_invalid_rates(value):
    with pytest.raises(d.ImportProblem):
        d.validate_rate(value)


def test_rate_lookup_direction_precision_and_no_credentials():
    session = Mock()
    result = requests.Response()
    result.status_code = 200
    result._content = b'{"base":"AUD","quote":"IDR","rate":12345.67890123456789,"date":"2024-10-23"}'
    session.get.return_value = result
    rate, source = d.fetch_rate(session)
    assert rate == Decimal("12345.67890123456789")
    assert "2024-10-23" in source
    assert "headers" not in session.get.call_args.kwargs
    assert session.get.call_args.kwargs["params"] == {"providers": "ecb"}


@pytest.mark.parametrize("data", [{}, [], {"base":"IDR", "quote":"AUD", "date":"2024-10-23", "rate":1},
    {"base":"AUD", "quote":"IDR", "date":"bad", "rate":1},
    {"base":"AUD", "quote":"IDR", "date":"9999-01-01", "rate":1},
    {"base":"AUD", "quote":"IDR", "date":"2024-10-23", "rate":0}])
def test_bad_rate_response(data):
    session = Mock()
    session.get.return_value = response(data)
    with pytest.raises(d.ImportProblem, match="invalid AUD/IDR"):
        d.fetch_rate(session)


def test_paginated_agentmail_and_original_forwarded_html():
    first, second = email(), email("bank_transfer")
    first["id"] = "<forward/a@example.com>"
    session = Mock()
    session.get.side_effect = [page([first], "page-two"), page([second]),
                               response(message_data(first)), response(message_data(second))]
    messages = d.fetch_emails(session, "secret", "inbox")
    assert [d.parse_email(message) for message in messages] == [d.parse_email(first), d.parse_email(second)]
    calls = session.get.call_args_list
    assert calls[0].args[0] == "https://api.agentmail.to/v0/inboxes/inbox/messages"
    assert calls[2].args[0].endswith("/messages/%3Cforward%2Fa%40example.com%3E")
    assert calls[0].kwargs["params"]["before"] == calls[1].kwargs["params"]["before"]
    assert calls[0].kwargs["params"]["before"].endswith("Z")
    assert calls[0].kwargs["params"]["ascending"] == "true"
    assert "page_token" not in calls[0].kwargs["params"]
    assert calls[1].kwargs["params"]["page_token"] == "page-two"
    assert "after" not in calls[0].kwargs["params"]
    assert "labels" not in calls[0].kwargs["params"]
    assert all(call.kwargs["headers"]["Authorization"] == "Bearer secret" for call in calls)
    assert all(call.kwargs["allow_redirects"] is False for call in calls)
    assert all(call.kwargs["timeout"] == (10, 30) for call in calls)


def test_plaintext_and_unknown_messages_require_review():
    message = email()
    unknown = {"id": "other", "subject": "Unknown notification"}
    detail = message_data(message)
    detail.pop("html")
    detail["text"] = "Plaintext"
    session = Mock()
    session.get.side_effect = [page([message, unknown]), response(detail)]
    batch = d.build_batch(d.fetch_emails(session, "key", "inbox"), SINCE)
    assert len(batch.issues) == 2
    assert session.get.call_count == 2


@pytest.mark.parametrize("data", [{"messages": []}, {"messages": [], "next_page_token": None},
                                  {"messages": [], "next_page_token": ""}])
def test_empty_agentmail_and_encoded_inbox(data):
    session = Mock()
    session.get.return_value = response(data)
    assert d.fetch_emails(session, "key", "sample@agentmail.to") == []
    assert session.get.call_args.args[0].endswith("/inboxes/sample%40agentmail.to/messages")


@pytest.mark.parametrize("pages", [
    [response({})],
    [response({"messages": None})],
    [page([], "next")],
    [page([email()], 123)],
    [page([email()], "next"), page([email()])],
    [page([email()], "next"), page([email("bank_transfer")], "next")],
    [response({"messages": [{}]})],
    [response({"messages": [{**message_data(email()), "inbox_id": "wrong"}]})],
])
def test_incomplete_or_inconsistent_pagination_fails(pages):
    session = Mock()
    session.get.side_effect = pages
    with pytest.raises(d.ImportProblem):
        d.fetch_emails(session, "key", "inbox")


@pytest.mark.parametrize("detail", [{}, [], {**message_data(email()), "message_id": "wrong"},
                                    {**message_data(email()), "inbox_id": "wrong"}])
def test_mismatched_message_fails(detail):
    session = Mock()
    session.get.side_effect = [page([email()]), response(detail)]
    with pytest.raises(d.ImportProblem, match="mismatched"):
        d.fetch_emails(session, "key", "inbox")


@pytest.mark.parametrize("error", [requests.Timeout("SECRET"), requests.ConnectionError("SECRET")])
def test_network_errors_do_not_leak_response_or_credentials(error):
    session = Mock()
    session.get.side_effect = error
    with pytest.raises(d.ImportProblem, match="request failed") as caught:
        d.fetch_emails(session, "SECRET", "inbox")
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("status", [301, 401, 404, 429, 500])
def test_http_errors(status):
    session = Mock()
    session.get.return_value = response({"private":"SECRET"}, status)
    with pytest.raises(d.ImportProblem, match=str(status)) as caught:
        d.fetch_emails(session, "SECRET", "inbox")
    assert "SECRET" not in str(caught.value)


def test_http_errors_identify_listing_or_message_retrieval():
    session = Mock()
    session.get.return_value = response({}, 404)
    with pytest.raises(d.ImportProblem, match="message listing returned HTTP 404") as caught:
        d.fetch_emails(session, "key", "inbox")
    assert "AGENTMAIL_INBOX_ID belongs to the account used by AGENTMAIL_API_KEY" in str(caught.value)
    session.get.side_effect = [page([email()]), response({}, 404)]
    with pytest.raises(d.ImportProblem, match="message retrieval returned HTTP 404"):
        d.fetch_emails(session, "key", "inbox")


def test_mid_retrieval_failure_never_returns_partial_batch():
    session = Mock()
    session.get.side_effect = [page([email(), email("bank_transfer")]), response(message_data(email())), requests.Timeout()]
    with pytest.raises(d.ImportProblem):
        d.fetch_emails(session, "key", "inbox")


def test_cli_preview_tsv_and_repeatability(capsys, monkeypatch):
    monkeypatch.setattr(d, "copy_tsv", Mock(side_effect=AssertionError("clipboard changed")))
    args = [str(SINCE), "--fixtures", str(FIXTURES), "--rate", str(RATE)]
    assert d.main(args) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "3 rows" in output.err and "479,950.00" in output.err
    for _ in range(2):
        assert d.main(args + ["--tsv"]) == 0
        assert capsys.readouterr().out == (FIXTURES / "expected.tsv").read_text()


def test_copy_runs_without_confirmation(monkeypatch, capsys):
    copy = Mock()
    monkeypatch.setattr(d, "copy_tsv", copy)
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("unexpected prompt")))
    assert d.main([str(SINCE), "--fixtures", str(FIXTURES), "--rate", str(RATE), "--copy"]) == 0
    copy.assert_called_once_with((FIXTURES / "expected.tsv").read_text())
    assert "[y/N]" not in capsys.readouterr().err


def test_review_items_block_export_unless_explicitly_skipped(tmp_path, capsys):
    (tmp_path/"good.json").write_text(json.dumps(email()))
    (tmp_path/"bad.json").write_text(json.dumps({"id":"bad", "subject":"unknown"}))
    args = [str(SINCE), "--fixtures", str(tmp_path), "--rate", str(RATE), "--tsv"]
    assert d.main(args) == 1
    output = capsys.readouterr()
    assert output.out == "" and 'REVIEW: bad [subject: "unknown"]' in output.err
    assert d.main(args + ["--skip-invalid"]) == 0
    assert capsys.readouterr().out.count("\n") == 1


def test_rate_failure_does_not_fall_back_or_copy(monkeypatch, capsys):
    monkeypatch.setattr(d, "fetch_rate", Mock(side_effect=d.ImportProblem("rate unavailable")))
    monkeypatch.setattr(d, "copy_tsv", Mock(side_effect=AssertionError("clipboard changed")))
    assert d.main([str(SINCE), "--fixtures", str(FIXTURES), "--copy"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "rate unavailable" in output.err


def test_missing_credentials(monkeypatch, capsys):
    monkeypatch.delenv("AGENTMAIL_API_KEY", raising=False)
    monkeypatch.delenv("AGENTMAIL_INBOX_ID", raising=False)
    assert d.main([str(SINCE)]) == 1
    assert "AGENTMAIL_API_KEY" in capsys.readouterr().err


def test_empty_batch_does_not_fetch_rate_or_touch_clipboard(monkeypatch, capsys):
    monkeypatch.setattr(d, "fetch_rate", Mock(side_effect=AssertionError("unneeded lookup")))
    monkeypatch.setattr(d, "copy_tsv", Mock(side_effect=AssertionError("clipboard changed")))
    assert d.main(["2024-11-01", "--fixtures", str(FIXTURES), "--copy"]) == 0
    assert "No matching" in capsys.readouterr().err


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("pbcopy",10),
                                  subprocess.CalledProcessError(1,"pbcopy")])
def test_clipboard_errors(error, monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    with pytest.raises(d.ImportProblem, match="pbcopy"):
        d.copy_tsv("data")


def test_clipboard_utf8(monkeypatch):
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    d.copy_tsv("Café")
    run.assert_called_once_with(["pbcopy"], input="Café".encode(), check=True, timeout=10)


def test_review_subjects_include_original_subject_and_missing_placeholder():
    message = change_field(email(), "Total", "invalid")
    message["subject"] = "Fwd: " + message["subject"]
    batch = d.build_batch([message, {"id": "unknown", "subject": "Fwd: Other Notice"}, {}], SINCE)
    assert f'[subject: "{message["subject"]}"]' in batch.issues[0]
    assert '[subject: "Fwd: Other Notice"]' in batch.issues[1]
    assert '[subject: "(no subject)"]' in batch.issues[2]


def test_review_subjects_are_single_line_and_bounded():
    batch = d.build_batch([{"id": "bad", "subject": "Notice\n\t\x1b" + "x" * 500}], SINCE)
    issue = batch.issues[0]
    assert "\n" not in issue and "\t" not in issue and "\x1b" not in issue
    assert len(issue) < 300


def test_live_cli_uses_agentmail_configuration(monkeypatch):
    monkeypatch.setenv("AGENTMAIL_API_KEY", "key")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "sample@agentmail.to")
    fetch = Mock(return_value=[])
    monkeypatch.setattr(d, "fetch_emails", fetch)
    assert d.main([str(SINCE)]) == 0
    assert fetch.call_args.args[1:] == ("key", "sample@agentmail.to")


def test_email_since_is_preserved_across_pages_without_filtering_bank_dates():
    first, second = email(), email('gopay_topup')
    session = Mock()
    session.get.side_effect = [page([first], 'next-page'), page([second]),
                               response(message_data(first)), response(message_data(second))]
    messages = d.fetch_emails(session, 'key', 'inbox', email_since=date(2024, 10, 25))
    calls = session.get.call_args_list
    assert calls[0].kwargs['params']['after'] == '2024-10-25T00:00:00Z'
    assert calls[1].kwargs['params']['after'] == calls[0].kwargs['params']['after']
    assert calls[1].kwargs['params']['page_token'] == 'next-page'
    batch = d.build_batch(messages, SINCE, date(2024, 11, 1))
    assert len(batch.transactions) == 2
    assert batch.transactions[0].occurred_at.date() == date(2024, 10, 3)


def test_email_since_cli_passes_cutoff_to_retrieval(monkeypatch, capsys):
    monkeypatch.setenv('AGENTMAIL_API_KEY', 'key')
    monkeypatch.setenv('AGENTMAIL_INBOX_ID', 'inbox')
    fetch = Mock(return_value=[email()])
    monkeypatch.setattr(d, 'fetch_emails', fetch)
    assert d.main(['2024-10-01', '--until', '2024-11-01', '--email-since', '2024-10-25',
                   '--rate', str(RATE), '--tsv']) == 0
    assert fetch.call_args.kwargs == {'email_since': date(2024, 10, 25)}
    output = capsys.readouterr()
    assert '2024-10-25 00:00 UTC (outer emails)' in output.err
    assert output.out.startswith('11/10/2024 14:35\t')


@pytest.mark.parametrize('args', [
    ['--email-since', 'not-a-date'],
    ['--email-since', '2024-02-30'],
    ['--email-since', '2024-10-25', '--fixtures', str(FIXTURES)],
])
def test_email_since_invalid_arguments_fail_before_network(args):
    with pytest.raises(SystemExit) as caught:
        d.main(['2024-10-01'] + args)
    assert caught.value.code == 2


@pytest.mark.parametrize("amount,rate,expected", [("-1.005", "1", "-1.01"), ("-1", "8", "-0.13"),
                                                 ("-100", "3", "-33.33")])
def test_numeric_aud_rounded_to_cents(amount, rate, expected):
    tx = replace(d.parse_email(email()), amount=Decimal(amount))
    row = next(csv.reader(io.StringIO(d.to_tsv([tx], Decimal(rate))), delimiter="\t"))
    assert row[2] == expected
