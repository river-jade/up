#!/usr/bin/env python3
"""Review Danamon notification emails and copy a batch to Google Sheets."""

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup


AGENTMAIL_URL = "https://api.agentmail.to/v0"
RATE_URL = "https://api.frankfurter.dev/v2/rate/aud/idr"
SUBJECTS = {
    "qris payment successful (do not reply)": "qris",
    "transfer to other account has been successful (no reply)": "transfer",
    "top up gopay successful (no reply)": "gopay",
}
MONTHS = {
    name: number
    for number, names in enumerate(
        ["january januari", "february februari", "march maret", "april",
         "may mei", "june juni", "july juli", "august agustus",
         "september", "october oktober", "november", "december desember"], 1
    )
    for name in names.split()
}
LABELS = {
    "status", "reference no", "to merchant", "payment date", "amount", "total",
    "date of transaction", "recipient name", "notes", "top up date", "biller",
    "top up amount", "admin fee",
}


class ImportProblem(ValueError):
    """A user-facing error, without raw email contents or credentials."""


@dataclass(frozen=True)
class Transaction:
    reference: str
    occurred_at: datetime
    amount: Decimal
    description: str
    kind: str


@dataclass
class Batch:
    transactions: list
    issues: list
    duplicates: int
    excluded: int


def clean_text(value):
    # Remove terminal escapes, invisible formatting and cell separators before display.
    value = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
    return " ".join(value.split())


def subject_kind(subject):
    if not isinstance(subject, str):
        raise ImportProblem("missing subject")
    subject = " ".join(subject.split()).casefold()
    subject = re.sub(r"^(?:(?:fwd?|re):\s*)+", "", subject)
    try:
        return SUBJECTS[subject]
    except KeyError:
        raise ImportProblem("unsupported subject; review this email manually") from None


def parse_amount(value):
    # Accept Indonesian grouping/decimal separators only; never guess US notation.
    match = re.fullmatch(r"Rp\s*([0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+)(?:,([0-9]{2}))?", value)
    if not match:
        raise ImportProblem("invalid rupiah amount")
    return Decimal(match[1].replace(".", "") + "." + (match[2] or "00"))


def parse_bank_date(value):
    match = re.fullmatch(r"([0-9]{1,2}) ([A-Za-z]+) ([0-9]{4}),? ([0-9]{2}):([0-9]{2})", value)
    if match:
        try:
            return datetime(int(match[3]), MONTHS[match[2].casefold()], int(match[1]),
                            int(match[4]), int(match[5]))
        except (ValueError, KeyError):
            pass
    raise ImportProblem("invalid bank transaction date")


def parse_email(email):
    if not isinstance(email, dict):
        raise ImportProblem("expected an email JSON object")
    kind = subject_kind(email.get("subject"))
    body = email.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ImportProblem("missing HTML body")
    soup = BeautifulSoup(body, "html.parser")
    for node in soup.find_all(["script", "style"]):
        node.decompose()
    fields = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) != 2 or any(cell.find("table") for cell in cells):
            continue
        label = " ".join(cells[0].get_text(" ", strip=True).split()).casefold().rstrip(".:")
        if label == "total amount":
            label = "total"
        if label in LABELS:
            if label in fields:
                raise ImportProblem("repeated transaction fields; forward one notification per email")
            fields[label] = " ".join(cells[1].get_text(" ", strip=True).split())

    def required(label):
        value = fields.get(label, "")
        if not value:
            raise ImportProblem("missing " + label)
        return value

    if required("status").casefold() != "successful":
        raise ImportProblem("transaction status is not Successful")
    reference = required("reference no")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]{0,127}", reference):
        raise ImportProblem("invalid bank reference")
    date_label = {"qris": "payment date", "transfer": "date of transaction", "gopay": "top up date"}[kind]
    occurred_at = parse_bank_date(required(date_label))
    amount_label = "top up amount" if kind == "gopay" else "amount"
    amount = parse_amount(required(amount_label))
    # A missing QRIS total can fall back to Amount; the other templates require Total.
    if kind == "qris" and "total" not in fields:
        total = amount
    else:
        total = parse_amount(required("total"))
    if amount <= 0 or total <= 0 or total < amount:
        raise ImportProblem("inconsistent or nonpositive debit amounts")
    if kind == "qris":
        description = required("to merchant")
    elif kind == "transfer":
        description = required("recipient name")
        if fields.get("notes"):
            description += " - " + fields["notes"]
        # The prose warning about a possible fee is deliberately not an amount field.
    else:
        if required("biller").casefold() != "gopay":
            raise ImportProblem("unsupported top-up biller")
        fee = parse_amount(required("admin fee"))
        if amount + fee != total:
            raise ImportProblem("GoPay total does not equal top-up amount plus confirmed fee")
        description = "GoPay top up"
        if fee:
            fee_text = format(fee, ",.0f") if fee == fee.to_integral_value() else format(fee, ",.2f")
            description += f" (incl. {fee_text} fee)"
    description = clean_text(description)
    if not description:
        raise ImportProblem("empty description")
    return Transaction(reference, occurred_at, -total, description, kind)


def build_batch(emails, since):
    transactions, issues, conflicted = {}, [], set()
    duplicates = 0
    for index, email in enumerate(emails, 1):
        identifier = email.get("id", f"item {index}") if isinstance(email, dict) else f"item {index}"
        identifier = clean_text(str(identifier))[:100]
        subject = email.get("subject") if isinstance(email, dict) else None
        subject = clean_text(subject) if isinstance(subject, str) else ""
        subject = subject or "(no subject)"
        context = f"{identifier} [subject: {json.dumps(subject[:200], ensure_ascii=False)}]"
        try:
            transaction = parse_email(email)
        except ImportProblem as exc:
            issues.append(f"{context}: {exc}")
            continue
        ref = transaction.reference
        previous = transactions.get(ref)
        if previous is None:
            transactions[ref] = transaction
        elif previous == transaction:
            duplicates += 1
        else:
            conflicted.add(ref)
            issues.append(f"{context}: conflicting copies of bank reference {ref}; all copies excluded")
    unique = [tx for ref, tx in transactions.items() if ref not in conflicted]
    included = sorted((tx for tx in unique if tx.occurred_at.date() >= since),
                      key=lambda tx: tx.occurred_at)
    return Batch(included, issues, duplicates, len(unique) - len(included))


def validate_rate(value):
    try:
        rate = Decimal(str(value))
    except InvalidOperation:
        raise ImportProblem("rate must be positive IDR per AUD") from None
    if not rate.is_finite() or not 0 < rate < Decimal("1000000000"):
        raise ImportProblem("rate must be positive IDR per AUD, below 1 billion")
    return rate


def get_json(session, url, service, **kwargs):
    try:
        # Never follow redirects carrying an API key to an unexpected host.
        response = session.get(url, timeout=(10, 30), allow_redirects=False, **kwargs)
        if response.status_code != 200:
            hint = ""
            if response.status_code == 404 and service == "AgentMail message listing":
                hint = " Check that AGENTMAIL_INBOX_ID belongs to the account used by AGENTMAIL_API_KEY."
            raise ImportProblem(f"{service} returned HTTP {response.status_code}; nothing exported.{hint}")
        return response.json(parse_float=Decimal)
    except requests.RequestException:
        raise ImportProblem(f"{service} request failed; check connection and credentials, then retry") from None
    except ValueError as exc:
        if isinstance(exc, ImportProblem):
            raise
        raise ImportProblem(f"{service} returned invalid JSON") from None


def fetch_rate(session):
    data = get_json(session, RATE_URL, "Frankfurter", params={"providers": "ecb"})
    try:
        if data["base"] != "AUD" or data["quote"] != "IDR":
            raise ValueError
        rate_date = date.fromisoformat(data["date"])
        if rate_date > datetime.now(timezone.utc).date():
            raise ValueError
        rate = validate_rate(data["rate"])
    except (KeyError, TypeError, ValueError):
        raise ImportProblem("Frankfurter returned an invalid AUD/IDR quote; use --rate to supply one") from None
    return rate, f"Frankfurter / ECB, published {rate_date.isoformat()}"


def fetch_emails(session, api_key, inbox_id):
    # List all available history; the bank-date boundary is applied after parsing.
    url = f"{AGENTMAIL_URL}/inboxes/{quote(inbox_id, safe='')}/messages"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    params = {"limit": 100, "ascending": "true",
              "before": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    previews, seen, tokens = [], set(), set()
    while True:
        data = get_json(session, url, "AgentMail message listing", headers=headers, params=dict(params))
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise ImportProblem("AgentMail returned an invalid page; nothing exported")
        for item in data["messages"]:
            if not isinstance(item, dict) or not isinstance(item.get("message_id"), str) or not item["message_id"]:
                raise ImportProblem("AgentMail returned a message without an ID")
            if item.get("inbox_id") != inbox_id:
                raise ImportProblem("AgentMail returned a message from a different inbox; nothing exported")
            if item["message_id"] in seen:
                raise ImportProblem("AgentMail repeated a message across pages; retry the import")
            seen.add(item["message_id"])
            previews.append(item)
        token = data.get("next_page_token")
        if token is None or token == "":
            break
        if not isinstance(token, str) or token in tokens or not data["messages"]:
            raise ImportProblem("AgentMail pagination is incomplete or repeated; nothing exported")
        tokens.add(token)
        params["page_token"] = token

    emails = []
    for preview in previews:
        email = {"id": preview["message_id"], "subject": preview.get("subject")}
        try:
            subject_kind(email["subject"])
        except ImportProblem:
            emails.append(email)  # Report unknown mail without retrieving its contents.
            continue
        message = get_json(session, f"{url}/{quote(email['id'], safe='')}",
                           "AgentMail message retrieval", headers=headers)
        if (not isinstance(message, dict) or message.get("message_id") != email["id"]
                or message.get("inbox_id") != inbox_id):
            raise ImportProblem("AgentMail returned an invalid or mismatched message; nothing exported")
        # extracted_html can remove the original bank notification from a forward.
        emails.append({**email, "subject": message.get("subject"), "body": message.get("html")})
    return emails


def load_fixtures(directory):
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise ImportProblem("no JSON emails found in the fixture directory")
    try:
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    except (OSError, ValueError):
        raise ImportProblem("could not read fixture JSON; nothing exported") from None


def to_tsv(transactions, rate):
    formula = f"=INDEX(B:B,ROW())/{validate_rate(rate):f}"
    output = io.StringIO()
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    for tx in transactions:
        description = clean_text(tx.description)
        if description.startswith(("=", "+", "-", "@")):
            description = "'" + description
        amount = format(tx.amount, "f")
        if "." in amount:
            amount = amount.rstrip("0").rstrip(".")
        writer.writerow([tx.occurred_at.strftime("%d/%m/%Y %H:%M"), amount, formula, "", description])
    return output.getvalue()


def copy_tsv(tsv):
    try:
        subprocess.run(["pbcopy"], input=tsv.encode("utf-8"), check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise ImportProblem("pbcopy failed or is unavailable; use --tsv to export instead") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("since", type=date.fromisoformat, help="inclusive bank transaction date, YYYY-MM-DD")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preview", action="store_true", help="review without copying (the default)")
    modes.add_argument("--copy", action="store_true", help="show preview, then ask before copying")
    modes.add_argument("--tsv", action="store_true", help="write TSV to stdout; preview/warnings go to stderr")
    parser.add_argument("--rate", type=validate_rate, help="manual IDR per AUD divisor; otherwise fetch latest ECB rate")
    parser.add_argument("--fixtures", type=Path, metavar="DIRECTORY", help="read local email JSON (id, subject, body) instead of the API")
    parser.add_argument("--skip-invalid", action="store_true", help="export valid rows despite reported review items")
    args = parser.parse_args(argv)
    try:
        with requests.Session() as session:
            if args.fixtures:
                emails = load_fixtures(args.fixtures)
            else:
                api_key, inbox_id = os.getenv("AGENTMAIL_API_KEY"), os.getenv("AGENTMAIL_INBOX_ID")
                if not api_key or not inbox_id:
                    raise ImportProblem("set AGENTMAIL_API_KEY and AGENTMAIL_INBOX_ID, or use --fixtures")
                print("Reading AgentMail inbox…", file=sys.stderr)
                emails = fetch_emails(session, api_key, inbox_id)
            batch = build_batch(emails, args.since)
            print(f"{len(emails)} emails; {batch.duplicates} duplicate copies; "
                  f"{batch.excluded} transactions before {args.since}.", file=sys.stderr)
            for issue in batch.issues:
                print("REVIEW: " + issue, file=sys.stderr)
            if not batch.transactions:
                print("No matching transactions. Nothing exported; clipboard unchanged.", file=sys.stderr)
                return 1 if batch.issues else 0
            if args.rate is not None:
                rate, source = args.rate, "manual override"
            else:
                rate, source = fetch_rate(session)
            print(f"Rate: 1 AUD = {rate:f} IDR ({source}).", file=sys.stderr)
            for tx in batch.transactions:
                print(f"{tx.occurred_at:%d/%m/%Y %H:%M}  {tx.amount:>16,.2f} IDR  {tx.description}", file=sys.stderr)
            total = sum((tx.amount for tx in batch.transactions), Decimal(0))
            print(f"{len(batch.transactions)} rows; total {total:,.2f} IDR / {total / rate:,.2f} AUD.", file=sys.stderr)
            if batch.issues and not args.skip_invalid:
                print("Resolve review items, or use --skip-invalid to export only the valid rows. Nothing exported.", file=sys.stderr)
                return 1
            if args.copy:
                print("Copy these rows to the clipboard? [y/N] ", end="", file=sys.stderr, flush=True)
                try:
                    confirmed = input().strip().casefold() in {"y", "yes"}
                except EOFError:
                    confirmed = False
                if confirmed:
                    copy_tsv(to_tsv(batch.transactions, rate))
                    print("Copied. Paste into column A of the first empty Danamon row.", file=sys.stderr)
                else:
                    print("Cancelled; clipboard unchanged.", file=sys.stderr)
            elif args.tsv:
                sys.stdout.write(to_tsv(batch.transactions, rate))
            else:
                print("Preview only; clipboard unchanged. Use --copy to review and copy a batch.", file=sys.stderr)
            return 0
    except (ImportProblem, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
