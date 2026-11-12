# tap-gokasa

`tap-gokasa` is a Singer tap for GoKasa sales exports, built with the
[Meltano Tap SDK](https://sdk.meltano.com). It reads a merchant's JSONL delivery from Azure
Blob Storage, merges it into the previous complete history, and emits the complete
`transactions` stream. It leaves all source files in place.

Storage and job handling are shared; a partner adapter owns filenames, schemas, parsing and
merging. **GoKasa is the only implemented adapter.** The package and executable remain
`tap-gokasa`.

## What one exact-file run does

1. Validates `partner_code`, credentials, the input filename and the history reference.
2. Reads only `input_blob` from `inbound_container`. It does not list other deliveries.
3. Checks every input line against the GoKasa schema before emitting any records.
4. Reads the exact `history_blob` from `history_container`, or starts with empty history when
   the caller explicitly supplies `history_blob: null`.
5. Merges by `transactionId`. A repeated transaction counts once. A delivery's version replaces
   the entire earlier record if it changed. Records are sorted by date and transaction ID.
6. Emits the complete history as Singer records. The surrounding pipeline writes the output.

Missing or invalid selected files fail the job; the tap never falls back to a different input or
silently discards missing history. Input and history are fully validated before any record is
emitted. Replaying the same delivery against its completed output preserves the same records.

The caller must supply the previous **successfully exported** history, serialize jobs for each
connection, and deduplicate completed upload events. The tap cannot determine remote job success
from blob contents. Replay preserves record content, but does not suppress downstream side effects.
Replaying an older delivery can overwrite newer corrections because deliveries have no version field.

## Configuration

| Setting | Requirement / default | Meaning |
|---|---|---|
| `partner_code` | Defaults to `GoKasa` | Adapter; unsupported values fail |
| `account_url` | Required | HTTPS account endpoint without credentials or query parameters |
| `merchant_id` | Required | Partner merchant ID, matched against the filename |
| `inbound_container` | Required | Trusted input container for this partner |
| `input_blob` | Recommended | Relative blob name, not a URL; selects exactly one delivery |
| `require_input_blob` | Defaults to `false`; set `true` for event-driven jobs | Rejects a missing file override instead of scanning |
| `history_container` | Required | Container containing previous complete exports |
| `history_prefix` | Required | This connection's history folder; a trailing `/` is enforced |
| `history_blob` | Required with `input_blob` | Exact previous successful output, or `null` for the initial delivery |
| `auth_method` | Defaults to `sas` for compatibility | Set to `service_principal` for application authentication |
| `azure_tenant_id` | Required for `service_principal` | Azure directory ID, distinct from any application/customer identifier |
| `azure_client_id` | Required for `service_principal` | Application ID |
| `azure_client_secret` | Required for `service_principal` | Application secret; keep in protected configuration |
| `inbound_sas_token` | Required for `sas` | Input container token with read access |
| `history_sas_token` | Required for `sas` | History container token with read access |
| `inbound_prefix` | Legacy only; defaults to `inbound/{merchant_id}/` | Scan prefix when `input_blob` is omitted |
| `archive_processed` | Deprecated; defaults to `false` | `true` fails validation; source files are never moved or deleted |

`history_prefix` supports `{tenant}` and `{tap}`, resolved from the job's `TENANT` and `TAP`
environment variables. These values must be single path components. An explicit `history_blob`
must be inside that folder and end with `transactions.jsonl` or `transactions.csv`.
The prefix is validated even for the initial run with `history_blob: null`. Supplying
`history_blob` without `input_blob` fails; omit both to use legacy scanning.

GoKasa filenames use `transactions_<merchant_id>_<delivery_id>.jsonl`. The last underscore
separates the merchant from a nonempty delivery ID, which cannot contain underscores or dots.
Merchant IDs may contain underscores. A numeric timestamp is not required. Folders are optional;
identity comes from the filename. Blob references must be relative, unencoded names without URL
queries, backslashes or traversal segments. The trusted caller maps the partner to its container
and the merchant to its connection; the tap does not perform customer lookup. Prefix validation
does not replace storage authorization.

### Service-principal authentication

Set `auth_method` to `service_principal` and provide all three `azure_*` fields. One
`ClientSecretCredential` is shared by the input and history clients. The Azure SDK obtains and
refreshes access tokens; the application secret still needs rotation. SAS fields are ignored in
this mode, and authentication failure does not fall back to SAS or a developer's login.

The processor needs read access to both containers, for example the **Storage Blob Data Reader**
role scoped to each container. No source write/delete permissions are needed. Exact-file runs do
not list blobs. Legacy scanning requires list access as well. The pipeline's separate output
writer needs its own authentication configuration and write permissions; changing tap credentials
does not change that writer.

Example first-run configuration (also in `.secrets/config.sample.json`):

```json
{
  "partner_code": "GoKasa",
  "auth_method": "service_principal",
  "account_url": "https://example.blob.core.windows.net",
  "azure_tenant_id": "<directory-id>",
  "azure_client_id": "<application-id>",
  "azure_client_secret": "<secret-from-protected-configuration>",
  "merchant_id": "M001",
  "require_input_blob": true,
  "inbound_container": "partner-input",
  "input_blob": "inbound/transactions_M001_delivery-1.jsonl",
  "history_container": "partner-output",
  "history_prefix": "history/connection-1/gokasa/",
  "history_blob": null
}
```

For the next job, replace `input_blob` with the new delivery and set `history_blob` to the last
successful output, such as `history/connection-1/gokasa/202611/job-1/transactions.jsonl`.
Pass these references as job-specific settings; keep secrets in protected persistent configuration.
Do not leave `history_blob: null` on subsequent jobs.

### Existing manual configurations

Omitting both `input_blob` and `history_blob` keeps the original prefix-scan mode. The tap lists
matching inputs, processes them in modification-time/name order, and selects the newest history
by modification time. This cannot distinguish a successful export from a failed job's output.
Use exact-file mode for event-driven integration.

Existing SAS configurations continue to work without `auth_method`. Both container tokens are
required; if the containers are the same, the tokens must also be the same. SAS requires list
permission when scanning. Inputs now remain in place by default. Old configurations containing
`archive_processed: true` must be changed to `false`.

## Output contract

The `transactions` stream is keyed on `transactionId`. Original field values, nested payments,
minor-unit amounts and cancellation links are preserved; the tap does not interpret refunds.
Null-valued optional fields are normalized to missing fields during merging.

| Field | Type | Notes |
|---|---|---|
| `transactionId` | string, required | Merge key |
| `payments` | array, required | Each payment has `paymentMethod`, integer `amount` in minor units, and `currency`; optional `acceptedAmount`, `acceptedCurrency` |
| `date` | `YYYY-MM-DD`, required | |
| `checkoutId` | string | |
| `cancel` | integer or null | On a sale, the checkout ID of its cancellation |

Empty files, unknown fields, missing required fields, wrong types, invalid dates and malformed JSON fail the
run with file/line context. Unknown fields must be added to the schema before accepting them;
otherwise the Singer SDK could drop them silently.

The pipeline should export a new `transactions.jsonl` beneath the connection's history folder,
without overwriting earlier jobs. JSONL is the preferred output; CSV history is also readable.
On history readback, the adapter restores payments encoded as JSON text, optional nulls, whole
numbers written as floats, numeric IDs, dates at midnight and pipeline-added columns. Other
invalid content fails the run.

## Code and local tests

| File | Responsibility |
|---|---|
| `tap_gokasa/tap.py` | Configuration, input/history selection and stream discovery |
| `tap_gokasa/configuration.py` | Credential requirements, blob references and path boundaries |
| `tap_gokasa/storage.py` | Read-only Azure clients and authentication |
| `tap_gokasa/streams.py` | Validate deliveries/history, merge and emit Singer records |
| `tap_gokasa/adapters.py` | Partner format selection and GoKasa filename checks |
| `tap_gokasa/schema.py`, `transactions.py`, `history.py` | GoKasa schema, parsing, merge and history restoration |

```bash
poetry install
poetry run pytest
poetry run tap-gokasa --about
poetry run tap-gokasa --config .secrets/config.json
```

Tests use synthetic transactions and in-memory storage or mocked Azure clients; they need no
Azure subscription. To compare a private real export through an initial run and a replay:

```bash
GOKASA_SAMPLE=path/to/export.jsonl poetry run pytest -k real_export
```

Keep real exports and credentials outside version control.
