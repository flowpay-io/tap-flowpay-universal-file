# tap-gokasa

`tap-gokasa` is a Singer tap for GoKasa sales exports, built with the
[Meltano Tap SDK](https://sdk.meltano.com).

GoKasa pushes one JSONL file per merchant to Azure Blob Storage. The first file holds the
merchant's history, and each later file holds only new transactions. The tap merges every waiting
file into the merchant's previous complete history and emits the new complete history.

## What one run does

1. Lists the merchant's waiting files: `inbound/{merchant_id}/*.jsonl` in the inbound container.
2. Checks every line against the stream schema (see below). A file that breaks it fails the run
   before any record goes out, and stays where it is.
3. Reads the newest previous history file (`transactions.jsonl` or `transactions.csv`) under
   `history_prefix` in the history container. If there is none, this is the merchant's first file.
4. Merges on `transactionId`. A transaction that is in more than one file counts once. If its
   content changed, the newer file wins and the tap logs it.
5. Emits the complete history as the `transactions` stream.
6. Moves the processed files from `inbound/` to `archive/` (see `archive_processed`).

The tap expects the pipeline that runs it to write the stream to a new
`.../transactions.jsonl` (or `.csv`) under `history_prefix`, and never to overwrite an earlier one.
The next run reads the newest of these files back as the previous history.

## Output

The `transactions` stream, keyed on `transactionId`. Each record is GoKasa's line unchanged; the
tap does not convert amounts or interpret refunds.

| Field | Type | Notes |
|---|---|---|
| `transactionId` | string, required | Merge key |
| `payments` | array, required | More than one for a split bill. Each: `paymentMethod`, `amount` (integer, minor units, negative for a refund, required), `currency` (required), `acceptedAmount`, `acceptedCurrency` |
| `date` | `YYYY-MM-DD`, required | |
| `checkoutId` | string | |
| `cancel` | integer | On a sale: the `checkoutId` of the refund that cancels it |

### Contract check

The stream schema in `tap_gokasa/schema.py` is also the contract for the input files. A file
fails on an unknown field (top level or in a payment), a missing required field, a wrong type, a
date that is not `YYYY-MM-DD`, or a line that is not JSON. The error names the file and lists up
to 20 problems with their line numbers.

Unknown fields fail the run instead of passing, because the Singer SDK would drop them silently.
To accept a new field, add it to the schema.

### Reading the history back

The history file is written by the pipeline, not by the tap, so it can differ from what the tap
emitted. The tap undoes `payments` as JSON text, `null` for a missing field, whole numbers as
floats, numeric ids as numbers, dates at midnight, and extra columns. Anything else fails the run.

## Configuration

| Setting | Required | Default | Description |
|---|---|---|---|
| `account_url` | yes | | Blob endpoint, e.g. `https://<account>.blob.core.windows.net` |
| `merchant_id` | yes | | GoKasa merchant ID. Selects the merchant's files. |
| `inbound_container` | yes | | Container GoKasa uploads to |
| `inbound_sas_token` | yes | | SAS token for the inbound container: read and list, plus write and delete when `archive_processed` is true |
| `inbound_prefix` | | `inbound/{merchant_id}/` | Where the merchant's files wait. Use `inbound/transactions_{merchant_id}_` if all merchants share one folder. |
| `archive_processed` | | `true` | Set to `false` if something else moves the files once the run has succeeded |
| `history_container` | yes | | Container the complete history files are written to |
| `history_sas_token` | yes | | SAS token for the history container: read and list |
| `history_prefix` | yes | | Folder with this connection's previous history files. `{tenant}` and `{tap}` are replaced with the `TENANT` and `TAP` environment variables of the job. |

Each container has its own SAS token, so each token can be scoped to that container. See
`.secrets/config.sample.json`.

## Development

```bash
poetry install
poetry run pytest
poetry run tap-gokasa --about
poetry run tap-gokasa --config .secrets/config.json
```

The tests replace Azure with an in-memory blob store (`tests/conftest.py`). To run a real export
through the tap twice (first file, then the same file merged into the first run's output), point
`GOKASA_SAMPLE` at it:

```bash
GOKASA_SAMPLE=path/to/export.jsonl poetry run pytest -k real_export
```
