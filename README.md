# Budget import scripts

## Shared environment (fish / VirtualFish)

Both scripts use the existing VirtualFish environment named `up`. Activate it
from either project directory; no repository-local `.venv` is needed:

```fish
vf activate up
python -m pip install -r requirements.txt
```

Run the install command from this repository. `requirements.txt` contains the
runtime dependencies for **both** importers; `requirements-dev.txt` adds the test
dependencies. On a machine without the environment, create it with `vf new up`
first. The scripts support Python 3.9 or newer; Danamon's tests pass in the shared
Python 3.9 environment and the original Python 3.13 development environment.

After activation, use the same `python` for `up.py` and `danamon.py`. VirtualFish
activation applies to the current shell, so run `vf activate up` in each new
terminal where you use the scripts. Use `vf deactivate` when finished.

## Up

This is a simple Python script to poll the Up Banking API and retrieve the most recent transactions. It prints the output to standard out in TSV format, with the same columns as the CSV export from the app. Much more convenient in my experience than exporting from the app, and importing the CSV file from there.

I usually run it like this `up.py 2024-05-17 | pbcopy`, which allows me to easily paste it into a spreadsheet.

Up reads `UP_ACCOUNT_ID` and `UP_ACCESS_TOKEN` from the environment. Both belong
in the same ignored `.env` used by Danamon. In fish, use `set -gx` assignments
as shown in `.env.fish.example`, then run:

```fish
vf activate up
source .env
python up.py 2026-10-01
```

Source `.env` in each new terminal; neither importer loads it automatically.
The previous `env.sh` is no longer needed. Missing Up settings stop the import
before contacting the API.

## Danamon

`danamon.py` reads notification emails for one Danamon source account from an
AgentMail inbox, previews a batch, and optionally copies five-column TSV to the
macOS clipboard. It does not access Google Sheets. `up.py` is independent and
unchanged by the Danamon implementation.

The required start date is **inclusive**, using the date printed in the bank's
transaction details. Transactions sort by that date and time, oldest first.
The bank's displayed local time is preserved; email arrival time and your Mac's
timezone do not determine ledger timestamps.

### Setup

Activate the shared `up` environment as described above.
For live imports, set `AGENTMAIL_API_KEY` to your AgentMail API key and
`AGENTMAIL_INBOX_ID` to your receiving inbox's **email address**. In fish, add
these settings to your existing `.env` file, then reload it:

```fish
set -gx AGENTMAIL_API_KEY 'your-api-key'
set -gx AGENTMAIL_INBOX_ID 'your-inbox@agentmail.to'
```

```fish
source .env
```

For a new configuration, copy `.env.fish.example` to `.env` and fill in the
values locally. Keep existing settings when adding these to a shared `.env`.
`.env.example` is available for bash/zsh users; bash-style assignments cannot be
sourced directly in fish. Old `MAILSLURP_*` settings are no longer used.

The script does not load credentials files automatically. `.env.fish`, `.env`,
legacy `env.sh` files, virtual environments, and private email directories are
ignored by Git. Keep real email exports in `private_emails/` or `raw_emails/`;
`fixtures/` is only for sanitized examples.

### Try the supplied examples without credentials or network access

```fish
python danamon.py 2024-10-03 --fixtures fixtures --rate 12636 --preview
python danamon.py 2024-10-03 --fixtures fixtures --rate 12636 --tsv
```

The second command produces exactly `fixtures/expected.tsv`: three rows totalling
**-479,950 IDR**. A manual `--rate` is necessary for a fully offline run; without it
even a fixture preview fetches the latest exchange rate.

### Import and paste

```fish
# Preview only; also the default when no output option is supplied:
python danamon.py 2026-10-20 --preview

# Preview, then answer y to copy the whole batch:
python danamon.py 2026-10-20 --copy

# Optional manual divisor instead of a network lookup:
python danamon.py 2026-10-20 --copy --rate 12636
```

Check the preview's timestamps, descriptions, count, total, and exchange-rate date.
After copying, select **column A of the first empty row** in the Danamon tab and
paste normally once. There is no header. The fields are:

| Column | Contents |
| --- | --- |
| A | Bank timestamp, `DD/MM/YYYY HH:MM` (24-hour time) |
| B | Negative numeric IDR debit |
| C | `=INDEX(B:B,ROW())/RATE`, with the batch's numeric rate substituted |
| D | Empty category for manual entry |
| E | Merchant or recipient/notes |

Normal paste is needed so column C becomes a formula. The format targets an
English (Australia) Sheet locale. On the first paste, check that A is recognized
as date/time values, B as numbers, C calculates AUD, and D remains empty. Format
column A as date and time if its existing date-only format hides the time. The
actual Sheet locale and paste behaviour have not been tested against your
spreadsheet.

No confirmation state is saved. Copying or pasting never marks a transaction as
imported. Repeating the same start date returns the same eligible transactions
while the inbox is unchanged; a fresh exchange-rate lookup may change AUD values.
Use `--rate` to reproduce a previous divisor. Handle overlap with existing Sheet
rows manually. Occasionally overlap dates to catch late notifications whose bank
dates precede the most recent import boundary.

`--tsv` writes only TSV to stdout, with the preview and diagnostics on stderr.
Prefer `--copy` over piping to `pbcopy`: it waits for a complete result and your
confirmation before touching the clipboard. Preview, cancellation, empty batches,
and retrieval/validation failures leave the clipboard unchanged. A clipboard
utility failure itself cannot guarantee the previous clipboard contents survived.

### Rates and review items

Each nonempty batch uses one latest daily **IDR per AUD** reference rate from
[Frankfurter](https://frankfurter.dev/), restricted to the ECB provider. No FX API
key is needed. The rate and publication date appear in the preview; weekends and
holidays can use the preceding published rate. This is a budgeting reference
rate, not your bank's actual conversion rate or a historical rate per transaction.
The numeric divisor is embedded in each formula so the pasted batch does not
change when future rates move. A lookup failure stops the export; supply `--rate`
to proceed manually. There is no automatic fallback to the old divisor.

Supported notifications are successful QRIS payments, outgoing bank transfers,
and GoPay top-ups. GoPay top-ups are expenses including the confirmed admin fee.
The transfer template's warning about a *possible* Rp2,500 fee is not a confirmed
charge and is not added. Missing/invalid fields, unsuccessful status, unsupported
subjects, and multiple transaction tables in one email require review. Incoming
payments, reversals, refunds, and other wallets are not supported yet.

Repeated copies of the same bank reference are deduplicated within a run. If
copies disagree about date, amount, description, or type, every copy of that
reference is excluded. Unknown and malformed messages are reported by email ID and subject
(subjects are sanitized and limited to 200 characters); raw bodies and credentials are not logged. Tabs/newlines and formula-like
descriptions are escaped for pasting.

Review items block export by default. After inspecting them, `--skip-invalid`
allows the valid rows to be copied/exported; warnings remain visible. Unknown
mail is reported even when its transaction date cannot be determined. API or
pagination failures cannot be bypassed with this flag.

### AgentMail and forwarding limitations

The script lists available messages in the configured inbox, including read
messages, following `next_page_token` until the last page. A fixed `before`
message-timestamp boundary keeps the scan bounded. There is no lower timestamp
filter: the bank-date boundary is applied **after** parsing, so forwarding old
notifications today works. This is intentionally simple and can be slow for a
large inbox. Deleted or unavailable messages cannot be recovered by this importer.
The listing uses AgentMail's defaults, which exclude spam, blocked,
unauthenticated, and trashed messages.

The implementation uses AgentMail's documented
[message listing](https://docs.agentmail.to/api-reference/inboxes/messages/list)
and [message retrieval](https://docs.agentmail.to/api-reference/inboxes/messages/get)
endpoints: `GET /v0/inboxes/{inbox_id}/messages` and
`GET /v0/inboxes/{inbox_id}/messages/{message_id}`, with Bearer authentication.
It reads the original `html` body; `extracted_html` can strip out the forwarded
bank notification. It only makes GET requests and never changes labels, sends or
deletes messages, or uses read status as import state. No additional SDK is needed.

If message listing returns **404**, check that `AGENTMAIL_INBOX_ID` is the inbox
email address associated with your `AGENTMAIL_API_KEY`. Reload the settings file
in your current shell after changing it. HTTP errors distinguish message listing
from message retrieval.

The fixtures are manually forwarded HTML notifications, one per email. Sender
addresses inside forwarded HTML are not proof of authenticity, and the parser
does not authenticate bank messages. Set up forwarding for the intended bank
notifications and inspect the preview. Automatically forwarded messages need a
real example to verify their format. Attached `.eml` files, plaintext-only mail,
and bundled threads are not supported as transaction input.

[Gmail forwarding filters only affect new messages](https://support.google.com/mail/answer/6579?hl=en-ca).
Historical notifications require a separate backfill step; do not assume enabling
a filter forwards existing history. Keep each backfilled notification as one
HTML email. Monthly Danamon statements remain the authoritative reconciliation
source because notification emails may not cover every transaction.

### Tests

```fish
vf activate up
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests use the sanitized fixtures and mock HTTP/clipboard calls; they do not need
credentials, contact live services, or modify your clipboard.
