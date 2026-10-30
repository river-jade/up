import csv
import io
import json
from datetime import datetime
from decimal import Decimal
from email.message import EmailMessage
from unittest.mock import Mock

import pytest

import danamon as d
from test_danamon import FIXTURES, RATE, SINCE, change_field, message_data, response


def virtual_account():
    return json.loads((FIXTURES / 'virtual_account' / 'payment.json').read_text())


def test_virtual_account_fixture_and_tsv():
    transaction = d.parse_email(virtual_account())
    assert transaction.kind == 'virtual_account'
    assert transaction.reference == 'VA-EXAMPLE-001'
    assert transaction.occurred_at == datetime(2024, 10, 23, 16, 40)
    assert transaction.amount == Decimal('-96000')
    assert transaction.description == 'Sample Payment Service - Sample Merchant'
    row = next(csv.reader(io.StringIO(d.to_tsv([transaction], RATE)), delimiter='\t'))
    assert row == ['23/10/2024 16:40', '-96000', '-7.60', '', transaction.description]


@pytest.mark.parametrize('label,value', [
    ('Status :', 'Failed'), ('Status :', None),
    ('Reference Number :', None), ('Reference Number :', '=bad'),
    ('Transaction Date :', None), ('Transaction Date :', '31 October 2024, 25:00'),
    ('Amount :', None), ('Amount :', 'Rp0,00'), ('Amount :', 'Rp-1,00'),
    ('Service Provider :', ''), ('Virtual Account Name :', None),
])
def test_virtual_account_invalid_fields_require_review(label, value):
    batch = d.build_batch([change_field(virtual_account(), label, value)], SINCE)
    assert not batch.transactions
    assert len(batch.issues) == 1
    assert 'Transfer Virtual Account Successful' in batch.issues[0]


def test_identical_provider_and_account_name_not_repeated():
    message = change_field(virtual_account(), 'Virtual Account Name :', 'sample payment service')
    assert d.parse_email(message).description == 'Sample Payment Service'


def test_duplicate_virtual_account_direct_and_eml():
    original = virtual_account()
    attached = EmailMessage()
    attached['Subject'] = 'Fwd: ' + original['subject']
    attached.set_content(original['body'], subtype='html')
    batch = d.build_batch([original, d.parse_eml(attached.as_bytes(), 'attachment 1')], SINCE)
    assert batch.issues == []
    assert len(batch.transactions) == 1
    assert batch.duplicates == 1


def test_virtual_account_live_retrieval_recognizes_subject():
    original = virtual_account()
    data = message_data(original)
    session = Mock()
    session.get.side_effect = [response({'messages': [data]}), response(data)]
    messages = d.fetch_emails(session, 'key', 'inbox')
    assert session.get.call_count == 2
    assert d.parse_email(messages[0]) == d.parse_email(original)


@pytest.mark.parametrize('rows', [
    '<tr><td>Admin Fee :</td><td>Rp2.500,00</td></tr>',
    '<tr><td>Total Amount :</td><td>Rp50.000,00</td></tr>',
    '<tr><td>Reference No.</td><td>another-reference</td></tr>',
])
def test_inconsistent_total_fee_or_duplicate_reference_requires_review(rows):
    message = virtual_account()
    message['body'] = message['body'].replace('</table>', rows + '</table>')
    assert d.build_batch([message], SINCE).issues


def test_explicit_total_and_fee_are_consistent():
    message = virtual_account()
    message['body'] = message['body'].replace('</table>',
        '<tr><td>Admin Fee :</td><td>Rp2.500,00</td></tr>'
        '<tr><td>Total Amount :</td><td>Rp98.500,00</td></tr></table>')
    assert d.parse_email(message).amount == Decimal('-98500')
