#!/usr/bin/env python3

import os
import csv

from decimal import Decimal
import requests
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from pymaybe import maybe


def convert_json_to_csv(start_date):
    account_id = os.getenv('UP_ACCOUNT_ID')
    access_token = os.getenv('UP_ACCESS_TOKEN')

    if not access_token:
        print("UP_ACCESS_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    if not account_id:
        print("UP_ACCOUNT_ID environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    base_url = f"https://api.up.com.au/api/v1/accounts/{account_id}/transactions"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    params = {
        "filter[since]": start_date,
        "page[size]": 100
    }

    writer = csv.writer(sys.stdout, delimiter='\t')
    # Expected CSV Headers (for documentation):
    # Time, BSB / Account Number, Account name, Transaction Type, Payee, Description, Category,
    # Tags, Subtotal (AUD), Currency, Subtotal (Transaction Currency), Round Up (AUD), Total (AUD),
    # Payment Method, Settled Date
    to_print = []
    while True:
        response = requests.get(base_url, headers=headers, params=params)
        if response.status_code != 200:
            print("Failed to fetch transactions:", response.text)
            break

        data = response.json()
        transactions = data['data']
        for transaction in transactions:
            transaction_time = transaction['attributes']['createdAt']
            payee = transaction['attributes']['description']
            description = transaction['attributes']['message']
            currency = maybe(transaction['attributes'])['foreignAmount']['currencyCode'].or_else(
                transaction['attributes']['amount']['currencyCode'])
            subtotal_aud = Decimal(transaction['attributes']['amount']['value'])
            subtotal_transaction_currency = maybe(transaction['attributes'])['foreignAmount']['value'].or_else(
                subtotal_aud)
            round_up_aud = Decimal(maybe(transaction['attributes']['roundUp'])['amount'].or_else(0))
            total_aud = subtotal_aud + round_up_aud
            payment_method = ''  # Payment method might not be available in Up Bank transactions
            settled_date = convert_from_rfc3339(transaction['attributes']['settledAt'])

            to_print.append([transaction_time, payee, description, currency, subtotal_transaction_currency, total_aud, settled_date])

        if data['links']['next']:
            base_url = data['links']['next']

            params = None  # Use next_url for pagination
        else:
            break
    for row in reversed(to_print):
        writer.writerow(row)
        


EASTERN_AUSTRALIA = ZoneInfo("Australia/Sydney")

def convert_to_rfc3339(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    dt = dt.replace(tzinfo=EASTERN_AUSTRALIA)
    return dt.isoformat()


def convert_from_rfc3339(rfc3339_str):
    if not rfc3339_str:
        return ""
    # Parse the RFC 3339 formatted string into a datetime object
    dt = datetime.fromisoformat(rfc3339_str.replace("Z", "+00:00"))

    # Convert to local timezone
    local_dt = dt.astimezone()

    # Convert the datetime object to a string in yyyy-mm-dd format
    date_str = local_dt.strftime("%Y-%m-%d")

    return date_str


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python script.py <start_date_in yyyy-mm-dd>")
        sys.exit(1)

    start_date = convert_to_rfc3339(sys.argv[1])
    convert_json_to_csv(start_date)
