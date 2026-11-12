# tap-flowpay-universal-file

`tap-flowpay-universal-file` is a Singer tap, built with the [Meltano Tap SDK](https://sdk.meltano.com),
for partners that push merchant sales exports to Azure Blob Storage as files. Each job reads one
delivered file, merges it into the merchant's previous complete history and emits the complete
history as one stream. Source files are left in place.

The partner's file format comes from settings: file type, filenames, the fields that identify a
record, the sort order and a JSON Schema of one record. A partner that delivers files this way
needs configuration, not code. GoKasa is built in as a preset, so configurations without these
settings keep working.

The project was previously called `tap-gokasa`. The `tap-gokasa` executable is kept as an alias.

## What one exact-file run does

1. Validates the settings, the input filename and the history reference before touching storage.
2. Reads only `input_blob` from `inbound_container`. It does not list other deliveries.
3. Checks every record against the configured schema before emitting anything. Fields that are not
   in the schema are ignored and listed in the log by name; their values are never logged.
4. Reads the exact `history_blob` from `history_container`, or starts with empty history when the
   caller explicitly supplies `history_blob: null`.
5. Merges on `key_properties`. A repeated record counts once. A delivery's version replaces the
   whole earlier record if it changed. Records are sorted by `sort_properties`, then by the key.
6. Emits the complete history as Singer records. The surrounding pipeline writes the output.

Missing or invalid selected files fail the job; the tap never falls back to a different input or
silently discards missing history. Input and history are fully validated before any record is
emitted. Replaying the same delivery against its completed output preserves the same records.

The caller must supply the previous **successfully exported** history, serialize jobs for each
connection, and deduplicate completed upload events. The tap cannot determine remote job success
from blob contents. Replay preserves record content, but does not suppress downstream side effects.
Replaying an older delivery can overwrite newer corrections because deliveries have no version field.

## Partner settings (file contract)

| Setting | Default | Meaning |
|---|---|---|
| `partner_code` | `GoKasa` | Label used in logs. Without `schema`, only `GoKasa` (the built-in preset) is accepted |
| `schema` | GoKasa preset | JSON Schema of one record, as an object or as JSON text |
| `key_properties` | Required with `schema` | Fields that identify a record; always required and, for text, non-empty |
| `sort_properties` | `key_properties` | Output order |
| `file_format` | `jsonl` | `jsonl` or `csv`; also the extension of delivered files |
| `file_prefix` | `transactions` | Delivered files are named `<file_prefix>_<merchant_id>_<delivery_id>.<file_format>` |
| `stream_name` | `transactions` | Output stream; history files are `<stream_name>.jsonl` or `<stream_name>.csv` |
| `csv_delimiter` | `,` | CSV only |
| `csv_decimal_separator` | `.` | CSV only, `.` or `,` |

Set either the complete contract (at least `schema` and `key_properties`) or none of it.
`key_properties` and `sort_properties` may be lists or JSON text. Settings are validated before
storage is used, with messages that contain no secrets.

**Schema.** The schema is an `object` with `properties`. Each field has exactly one type, with
`null` optionally allowed in addition: `string` (optionally `format: date` or
`format: date-time`), `integer`, `number`, `boolean`, and for JSONL also `array` and `object`.
Key and sort fields must be top-level single values. The root must be an object, never null.
Keys must be non-null; text keys must be non-empty even if the field declares `minLength: 0`.
Arrays must declare one `items` schema. An object without `properties` has no declared fields,
so all of its child fields are ignored and counted. Numeric values must be finite.

**Fields that are not configured** are removed before checking, at any depth, and listed in the
log with how many records had each. Add a field to the schema when it is needed; from then on it is
kept. Records processed earlier do not have it unless the partner delivers them again.

**Configured fields are strict.** A missing required field, a value of the wrong type, an
unreadable line or row, or a file without records fails the job before anything is emitted, with
file and line context (the first 20 problems and a count).

**JSONL** has one JSON object per line; values must already have the configured types. UTF-8 with
an optional BOM and CRLF line ends are accepted.

**CSV** needs a header row and uses standard quoting. Values are converted from text by the
configured type: an empty cell is a missing field, `integer` accepts whole numbers only, `number`
uses `csv_decimal_separator` (with a decimal comma, a dot is rejected rather than guessed to be a
thousands separator), `boolean` accepts `true`/`false` and `1`/`0` in any case. Duplicate column
names, malformed quoting and rows with more cells than the header fail.

**Dates and timestamps.** `date` is `YYYY-MM-DD`. `date-time` is ISO 8601 with `T` or a space
between date and time, with or without seconds, fractions and a UTC offset (`Z` or `+02:00`).
Values are checked, never rewritten.

Example: a partner exporting CSV with a semicolon delimiter and a decimal comma, one row per receipt
line:

```json
{
  "partner_code": "ExamplePOS",
  "file_format": "csv",
  "csv_delimiter": ";",
  "csv_decimal_separator": ",",
  "key_properties": ["receiptId", "line"],
  "sort_properties": ["soldAt"],
  "schema": {
    "type": "object",
    "properties": {
      "receiptId": {"type": "string"},
      "line": {"type": "integer"},
      "soldAt": {"type": "string", "format": "date-time"},
      "amount": {"type": "number"},
      "refund": {"type": ["boolean", "null"]},
      "note": {"type": ["string", "null"]}
    },
    "required": ["soldAt", "amount"]
  }
}
```

### GoKasa

These settings are exactly what the built-in preset uses. Configuring them explicitly gives the
same output as omitting them. GoKasa delivers JSONL; `amount` and `acceptedAmount` are integers in
haléř, and `cancel` on a sale is the checkout ID of its cancellation.

<!-- gokasa-settings -->
```json
{
  "partner_code": "GoKasa",
  "file_format": "jsonl",
  "file_prefix": "transactions",
  "stream_name": "transactions",
  "key_properties": [
    "transactionId"
  ],
  "sort_properties": [
    "date",
    "transactionId"
  ],
  "schema": {
    "type": "object",
    "properties": {
      "transactionId": {
        "type": [
          "string"
        ],
        "description": "Merge key"
      },
      "payments": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "paymentMethod": {
              "type": [
                "string",
                "null"
              ],
              "description": "CASH, CREDIT_CARD or BANK_TRANSFER"
            },
            "amount": {
              "type": [
                "integer"
              ],
              "description": "Haléř; negative for a refund"
            },
            "currency": {
              "type": [
                "string"
              ]
            },
            "acceptedAmount": {
              "type": [
                "integer",
                "null"
              ]
            },
            "acceptedCurrency": {
              "type": [
                "string",
                "null"
              ]
            }
          },
          "required": [
            "amount",
            "currency"
          ]
        },
        "description": "More than one for a split bill"
      },
      "date": {
        "type": [
          "string"
        ],
        "format": "date"
      },
      "checkoutId": {
        "type": [
          "string",
          "null"
        ]
      },
      "cancel": {
        "type": [
          "integer",
          "null"
        ],
        "description": "On a sale: the checkoutId of the refund that cancels it"
      }
    },
    "required": [
      "transactionId",
      "payments",
      "date"
    ]
  }
}
```

## Job and storage settings

| Setting | Requirement / default | Meaning |
|---|---|---|
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
must be inside that folder and be named `<stream_name>.jsonl` or `<stream_name>.csv`.
The prefix is validated even for the initial run with `history_blob: null`. Supplying
`history_blob` without `input_blob` fails; omit both to use legacy scanning.

In filenames, the last underscore separates the merchant from a nonempty delivery ID, which cannot
contain underscores or dots. Merchant IDs may contain underscores. Folders are optional; identity
comes from the filename. Blob references must be relative, unencoded names without URL queries,
backslashes or traversal segments. The trusted caller maps the partner to its container and the
merchant to its connection; the tap does not perform customer lookup. Prefix validation does not
replace storage authorization.

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

Example first-run configuration for GoKasa (also in `.secrets/config.sample.json`):

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
Pass these references as job-specific settings; keep the file contract and secrets in persistent
configuration. Do not leave `history_blob: null` on subsequent jobs.

### Existing manual configurations

Omitting both `input_blob` and `history_blob` keeps the original prefix-scan mode. The tap lists
matching inputs, processes them in modification-time/name order, and selects the newest history
by modification time. This cannot distinguish a successful export from a failed job's output.
Use exact-file mode for event-driven integration.

Existing SAS configurations continue to work without `auth_method`. Both container tokens are
required; if the containers are the same, the tokens must also be the same. SAS requires list
permission when scanning. Inputs remain in place. Old configurations containing
`archive_processed: true` must be changed to `false`.

## History readback

The pipeline should export a new `<stream_name>.jsonl` beneath the connection's history folder,
without overwriting earlier jobs. JSONL is preferred; CSV history (comma-separated) is also
readable. The pipeline may change how values look, so readback restores them by the configured
types: nested values written as JSON text, optional nulls, numbers and booleans written as text,
whole numbers written as floats, numeric IDs, dates at midnight and pipeline-added columns.
Integer history values are restored exactly, including large IDs written as integer, decimal
or exponent text; fractional values are rejected rather than rounded. Anything else that does
not fit fails the run.

History is checked for types and keys, but not for fields that became required after it was
written, so making a field required does not break the next job.

## Code and local tests

| File | Responsibility |
|---|---|
| `tap_flowpay_universal_file/tap.py` | Settings, input/history selection and stream discovery |
| `tap_flowpay_universal_file/contract.py` | Partner file contract from settings, and the GoKasa preset |
| `tap_flowpay_universal_file/configuration.py` | Credential requirements, blob references and path boundaries |
| `tap_flowpay_universal_file/storage.py` | Read-only Azure clients and authentication |
| `tap_flowpay_universal_file/streams.py` | Read deliveries and history, merge and emit Singer records |
| `tap_flowpay_universal_file/schema.py` | Schema loading, record checks, ignored fields, timestamp rule, GoKasa schema |
| `tap_flowpay_universal_file/records.py`, `history.py` | JSONL/CSV reading, merging, history restoration |

```bash
poetry install
poetry run pytest
poetry run tap-flowpay-universal-file --about
poetry run tap-flowpay-universal-file --config .secrets/config.json
```

Tests use synthetic records and in-memory storage or mocked Azure clients; they need no Azure
subscription. To compare a private real GoKasa export through an initial run and a replay, and
through explicit settings against the preset:

```bash
GOKASA_SAMPLE=path/to/export.jsonl poetry run pytest -k real_export
```

Keep real exports and credentials outside version control.
