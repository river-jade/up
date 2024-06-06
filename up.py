#!/usr/bin/env python3

import os
import csv

from decimal import Decimal
import requests
import sys
from datetime import datetime
from pymaybe import maybe


def convert_json_to_csv(start_date):
    base_url = "https://api.up.com.au/api/v1/transactions"
    access_token = os.getenv('UP_ACCESS_TOKEN')

    if not access_token:
        print("UP_ACCESS_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    params = {
        "filter[since]": start_date,
        "page[size]": 100
    }

    writer = csv.writer(sys.stdout, delimiter='\t')
    writer.writerow(
        ['Time', 'BSB / Account Number', 'Account name', 'Transaction Type', 'Payee', 'Description', 'Category',
         'Tags', 'Subtotal (AUD)', 'Currency', 'Subtotal (Transaction Currency)', 'Round Up (AUD)', 'Total (AUD)',
         'Payment Method', 'Settled Date'])

    while True:
        response = requests.get(base_url, headers=headers, params=params)
        if response.status_code != 200:
            print("Failed to fetch transactions:", response.text)
            break

        data = response.json()
        transactions = data['data']
        for transaction in reversed(transactions):
            transaction_time = transaction['attributes']['createdAt']
            payee = transaction['attributes']['description']
            description = transaction['attributes']['rawText']
            category = maybe(transaction['relationships']['category']['data'])['attributes']['title'].or_else(None)
            tags = ', '.join([tag['attributes']['name'] for tag in transaction['relationships']['tags']['data']])
            subtotal_aud = Decimal(transaction['attributes']['amount']['value'])
            currency = maybe(transaction['attributes'])['foreignAmount']['currencyCode'].or_else(
                transaction['attributes']['amount']['currencyCode'])
            subtotal_transaction_currency = maybe(transaction['attributes'])['foreignAmount']['value'].or_else(
                subtotal_aud)
            round_up_aud = Decimal(maybe(transaction['attributes']['roundUp'])['amount'].or_else(0))
            total_aud = subtotal_aud + round_up_aud
            payment_method = ''  # Payment method might not be available in Up Bank transactions
            settled_date = convert_from_rfc3339(transaction['attributes']['settledAt'])

            writer.writerow(
                [transaction_time, '', '', '', payee, description, category, tags, subtotal_aud, currency,
                 subtotal_transaction_currency, round_up_aud, total_aud, payment_method, settled_date])

        if data['links']['next']:
            base_url = data['links']['next']
            params = None  # Use next_url for pagination
        else:
            break


def convert_to_rfc3339(date_str):
    # Convert the input string to a datetime object
    dt = datetime.strptime(date_str, "%Y-%m-%d")

    # Convert the datetime object to RFC 3339 format
    rfc3339_str = dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    return rfc3339_str


def convert_from_rfc3339(rfc3339_str):
    if not rfc3339_str:
        return ""
    # Parse the RFC 3339 formatted string into a datetime object
    dt = datetime.fromisoformat(rfc3339_str.replace("Z", "+00:00"))

    # Convert the datetime object to a string in yyyy-mm-dd format
    date_str = dt.strftime("%Y-%m-%d")

    return date_str


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python script.py <start_date_in yyyy-mm-dd>")
        sys.exit(1)

    start_date = convert_to_rfc3339(sys.argv[1])
    convert_json_to_csv(start_date)
