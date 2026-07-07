# logger

The Android field-logger app (placeholder — no code yet).

The logger records cellular measurements while driving rural routes and uploads
them in batches to the ingestion API's `POST /v1/measurements` endpoint.

Design notes for when this is built out:

- **Client-generated `sample_id`** (UUID) per measurement so uploads are
  idempotent and safe to retry over flaky links — the server dedupes on it.
- **`session_id`** (UUID) per drive, sent once at the batch level.
- **Dead zones are data.** When there is no serving cell, still emit a sample
  with `network_type = "none"` and null `rsrp`/`rsrq`/`sinr`. Absence of
  coverage is the primary signal this project maps.
- **Store-and-forward.** Buffer samples locally (there is often no connectivity
  in the field) and flush batches when a link is available.
