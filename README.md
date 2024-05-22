This is a simple Python script to poll the Up Banking API and retrieve the most recent transactions. It prints the output to standard out in TSV format, with the same columns as the CSV export from the app. Much more convenient in my experience than exporting from the app, and importing the CSV file from there.

I usually run it like this `up.py 2024-05-17 | pbcopy`, which allows me to easily paste it into a spreadsheet.
