# Sanitized Danamon sample messages

These are minimal MailSlurp-like JSON objects. Their `body` HTML retains the bank's original table and label structure, and the transaction amounts and dates are fictional. Personal email addresses, account names/numbers, PANs, phone numbers, transaction reference identifiers, and MailSlurp inbox identifiers have been replaced with synthetic placeholders. The outer sender is a synthetic Gmail forwarder; the original Danamon sender remains in the forwarded HTML.

- `qris.json`: QRIS purchase at Example Cafe
- `bank_transfer.json`: outgoing transfer to Example Recipient, note `Example invoice`
- `gopay_topup.json`: GoPay top-up with a **confirmed** Rp1,500 fee

The full original MailSlurp JSON objects have additional transport/header fields; parser code should rely primarily on `subject` and HTML `body` for these sample formats.

These historical examples retain their original MailSlurp envelope. The importer normalizes AgentMail messages to the same `id`, `subject`, and HTML `body` fields, so these fixtures remain usable offline. AgentMail transport behavior is tested separately with mocked API responses.

`virtual_account/payment.json` is a sanitized example based on an original
virtual-account payment received as an `.eml` attachment. It preserves the actual
field labels, with fictional timestamp, amount, and references, account numbers,
provider, and account name. It is in a subdirectory so the original three-message
`expected.tsv` batch remains unchanged. To preview this example offline:

```sh
python danamon.py 2026-10-01 --fixtures fixtures/virtual_account --rate 12636
```
