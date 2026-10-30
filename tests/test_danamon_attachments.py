from datetime import date
from email.message import EmailMessage
from unittest.mock import Mock

import pytest
import requests

import danamon as d
from test_danamon import FIXTURES, SINCE, RATE, change_field, email, message_data, response


def eml(name="qris", charset="utf-8", cte="quoted-printable"):
    original = email(name)
    message = EmailMessage()
    message["Subject"] = original["subject"]
    message.set_content("Plain text alternative")
    message.add_alternative(original["body"], subtype="html", charset=charset, cte=cte)
    return message


def download(content, status=200):
    result = Mock(status_code=status)
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = [content]
    return result


def attachment_setup(names=("qris",)):
    attachments = [{"attachment_id": f"att/{i}", "filename": f"{name}.eml",
                    "content_type": "message/rfc822"} for i, name in enumerate(names)]
    wrapper = {"inbox_id": "inbox", "message_id": "<batch@example.com>",
               "subject": "October backfill", "attachments": attachments}
    metadata = [{"attachment_id": item["attachment_id"],
                 "download_url": f"https://downloads.example.com/{i}?signature=secret"}
                for i, item in enumerate(attachments)]
    return wrapper, metadata


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("unexpected network call")))


@pytest.mark.parametrize("charset,cte", [("utf-8", "quoted-printable"), ("utf-8", "base64"),
                                         ("iso-8859-1", "base64")])
def test_decode_attached_original(charset, cte):
    parsed = d.parse_eml(eml(charset=charset, cte=cte).as_bytes(), "attachment 1")
    assert parsed["id"] == "attachment 1"
    assert d.parse_email(parsed) == d.parse_email(email())


def test_bulk_attachments_reuse_parser_and_deduplicate_direct_copy(monkeypatch):
    names = ("qris", "bank_transfer", "gopay_topup")
    wrapper, metadata = attachment_setup(names)
    direct = message_data(email())
    session = Mock()
    session.get.side_effect = [response({"messages": [wrapper, direct]}),
                              *(response(item) for item in metadata), response(direct)]
    get = Mock(side_effect=[download(eml(name).as_bytes()) for name in names])
    monkeypatch.setattr(requests, "get", get)
    messages = d.fetch_emails(session, "API-SECRET", "inbox")
    assert len(messages) == 4  # The wrapper is not an invalid transaction.
    batch = d.build_batch(messages, SINCE)
    assert batch.issues == []
    assert batch.duplicates == 1
    assert len(batch.transactions) == 3
    assert session.get.call_args_list[1].args[0].endswith(
        "/messages/%3Cbatch%40example.com%3E/attachments/att%2F0")
    for call in get.call_args_list:
        assert call.kwargs == {"timeout": (10, 30), "allow_redirects": False, "stream": True}
        assert "API-SECRET" not in str(call)


@pytest.mark.parametrize("attachment,expected", [
    ({"filename": "bank.EML", "content_type": "application/octet-stream"}, True),
    ({"content_type": "message/rfc822; name=bank"}, True),
    ({"filename": "logo.png", "content_type": "image/png"}, False),
    ({"filename": "statement.pdf"}, False),
])
def test_email_attachment_detection(attachment, expected):
    assert d.is_email_attachment(attachment) is expected


def test_unknown_attachment_subject_and_plaintext_have_source_in_warning():
    message = EmailMessage()
    message["Subject"] = "Unknown transaction"
    message.set_content("Body")
    unknown = d.parse_eml(message.as_bytes(), "attachment 2 (other.eml) in batch")
    del message["Subject"]
    message["Subject"] = email()["subject"]
    plain = d.parse_eml(message.as_bytes(), "attachment 3 (plain.eml) in batch")
    batch = d.build_batch([unknown, plain], SINCE)
    assert len(batch.issues) == 2
    assert 'attachment 2 (other.eml)' in batch.issues[0]
    assert 'subject: "Unknown transaction"' in batch.issues[0]
    assert 'missing HTML body' in batch.issues[1]


@pytest.mark.parametrize("content", [b"not an email", b"", b"Subject: broken\nContent-Type: multipart/mixed; boundary=x\n\nno boundary"])
def test_malformed_attachment_requires_review(content):
    batch = d.build_batch([d.parse_eml(content, "broken.eml")], SINCE)
    assert not batch.transactions
    assert len(batch.issues) == 1


def test_unknown_charset_requires_review():
    content = b'Subject: QRIS Payment Successful (Do Not Reply)\nContent-Type: text/html; charset=unknown-charset\n\n<html>hello</html>'
    batch = d.build_batch([d.parse_eml(content, "bad.eml")], SINCE)
    assert "unsupported character encoding" in batch.issues[0]


def test_nested_attached_emails_are_flagged_instead_of_silently_lost():
    message = eml()
    message.add_attachment(eml("bank_transfer"))
    batch = d.build_batch([d.parse_eml(message.as_bytes(), "nested.eml")], SINCE)
    assert not batch.transactions
    assert "nested attached emails" in batch.issues[0]


def test_inline_images_are_not_downloaded(monkeypatch):
    direct = message_data(email())
    direct["attachments"] = [{"attachment_id": "logo", "content_type": "image/png"}]
    session = Mock()
    session.get.side_effect = [response({"messages": [direct]}), response(direct)]
    get = Mock(side_effect=AssertionError("unneeded download"))
    monkeypatch.setattr(requests, "get", get)
    assert len(d.fetch_emails(session, "key", "inbox")) == 1
    get.assert_not_called()


@pytest.mark.parametrize("status", [301, 403, 404, 500])
def test_attachment_download_failure_stops_whole_batch(status, monkeypatch):
    wrapper, metadata = attachment_setup(("qris", "bank_transfer"))
    session = Mock()
    session.get.side_effect = [response({"messages": [wrapper]}), *(response(item) for item in metadata)]
    monkeypatch.setattr(requests, "get", Mock(side_effect=[download(eml().as_bytes()), download(b"private", status)]))
    with pytest.raises(d.ImportProblem, match=f"HTTP {status}"):
        d.fetch_emails(session, "API-SECRET", "inbox")


@pytest.mark.parametrize("metadata", [{}, {"attachment_id": "wrong"},
    {"attachment_id": "att/0", "download_url": "http://example.com"},
    {"attachment_id": "att/0", "download_url": "https://[invalid"},
    {"attachment_id": "att/0", "download_url": "https://user:pass@example.com"},
    {"attachment_id": "att/0", "download_url": None}])
def test_invalid_attachment_metadata_blocks_download(metadata, monkeypatch):
    wrapper, _ = attachment_setup()
    session = Mock()
    session.get.side_effect = [response({"messages": [wrapper]}), response(metadata)]
    get = Mock(side_effect=AssertionError("untrusted download"))
    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(d.ImportProblem):
        d.fetch_emails(session, "key", "inbox")
    get.assert_not_called()


def test_download_errors_do_not_leak_signed_url(monkeypatch):
    wrapper, metadata = attachment_setup()
    session = Mock()
    session.get.side_effect = [response({"messages": [wrapper]}), response(metadata[0])]
    monkeypatch.setattr(requests, "get", Mock(side_effect=requests.Timeout("signature=SECRET")))
    with pytest.raises(d.ImportProblem, match="download failed") as caught:
        d.fetch_emails(session, "key", "inbox")
    assert "SECRET" not in str(caught.value)


def test_oversize_attachment_blocks_export(monkeypatch):
    wrapper, metadata = attachment_setup()
    session = Mock()
    session.get.side_effect = [response({"messages": [wrapper]}), response(metadata[0])]
    monkeypatch.setattr(d, "MAX_ATTACHMENT_BYTES", 10)
    monkeypatch.setattr(requests, "get", Mock(return_value=download(b"x" * 11)))
    with pytest.raises(d.ImportProblem, match="exceeds 25 MB"):
        d.fetch_emails(session, "key", "inbox")


def test_exclusive_end_date_and_cli_preview(capsys):
    messages = [email("qris"), email("bank_transfer"), email("gopay_topup")]
    batch = d.build_batch(messages, SINCE, date(2024, 10, 17))
    assert len(batch.transactions) == 2
    assert batch.excluded == 1
    assert d.main([str(SINCE), "--until", "2024-10-17", "--fixtures", str(FIXTURES), "--rate", str(RATE), "--tsv"]) == 0
    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 2
    assert "1 transactions outside the date range" in output.err


@pytest.mark.parametrize("until", ["2024-10-01", "2024-10-03"])
def test_invalid_date_range_rejected_before_network(until):
    with pytest.raises(SystemExit) as caught:
        d.main([str(SINCE), "--until", until])
    assert caught.value.code == 2


def test_october_range_excludes_september_and_november():
    september = change_field(email(), "Payment Date", "30 September 2024 23:59")
    october = change_field(email(), "Payment Date", "31 Oktober 2024 23:59")
    change_field(october, "Reference No", "october")
    november = change_field(email(), "Payment Date", "1 November 2024 00:00")
    change_field(november, "Reference No", "november")
    batch = d.build_batch([september, october, november], date(2024, 10, 1), date(2024, 11, 1))
    assert [tx.reference for tx in batch.transactions] == ["october"]
    assert batch.excluded == 2


def test_invalid_attached_email_blocks_copy_unless_explicitly_skipped(monkeypatch, capsys):
    monkeypatch.setenv("AGENTMAIL_API_KEY", "key")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "inbox")
    monkeypatch.setattr(d, "fetch_emails", Mock(return_value=[email(), d.parse_eml(b"broken", "attachment 2")]))
    copy = Mock()
    monkeypatch.setattr(d, "copy_tsv", copy)
    args = [str(SINCE), "--rate", str(RATE), "--copy"]
    assert d.main(args) == 1
    copy.assert_not_called()
    assert "attachment 2" in capsys.readouterr().err
    assert d.main(args + ["--skip-invalid"]) == 0
    copy.assert_called_once_with(d.to_tsv([d.parse_email(email())], RATE))
