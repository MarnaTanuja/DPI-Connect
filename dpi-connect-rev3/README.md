# DPI Connect

A full-stack app for organizations to request data connections from each
other, get an immediate schema-similarity score, go through a consent
workflow, and — once approved — pull a combined dataset over a signed
secure-exchange layer.

**Workflow this implements:**
1. Org **A** and Org **B** each add a dataset — by **uploading a file** (CSV, TSV,
   JSON, JSONL, Excel `.xlsx`) or by **pasting** rows (straight from a spreadsheet,
   as CSV, or as JSON). A preview shows the detected columns and value kinds
   before anything is saved.
2. **A** requests a connection to **B**. Every field of both datasets is scored
   against every field of the other (see *Confidence scoring* below) and a
   **FULL OUTER JOIN** plan is produced: matched columns merged once, everything
   else kept. The join key is auto-detected from the data.
3. The request lands in the **platform admin's review queue**. The admin sees both
   organizations' mappings, each with its confidence and the evidence behind it,
   plus field metadata of both datasets (never raw rows), and **approves or
   rejects** the connection with a note.
4. On approval, **B** reviews the same report and consents (signs a short
   agreement) — or declines. (Set `REQUIRE_COUNTERPARTY_CONSENT=0` to make admin
   approval alone sufficient.)
5. A simulated **X-Road secure handshake** opens the channel.
6. **A** pulls the combined dataset: matched rows merged, A-only rows kept,
   B-only rows kept; row counts never multiply. Each combined column is tagged
   with which A field and which B field fed it, and the confidence.
7. Every step, including the admin's decision, is written to a signed audit trail.

Status flow: `PENDING_ADMIN_REVIEW → PENDING_CONSENT → APPROVED → ACTIVE`, or
`REJECTED_BY_ADMIN` / `REJECTED`.

## Confidence scoring

Mappings used to be reported at 100% because the reconciler declared whatever it
merged to be correct. Now each field pair gets a score from independent signals
(`mapping_engine/field_matcher.py`, `semantic.py`):

| Signal | What it measures |
|---|---|
| lexical | spelling similarity of the names |
| semantic | meaning: tokenised names, abbreviations (`dob`→date+birth), a concept lexicon (`citizen`/`owner`/`taxpayer`→party; `id`/`number`/`reference`→identifier); opposing qualifiers (`first`/`last`, `valid_from`/`valid_to`) are penalised; optional Mistral embeddings of the *names only* |
| structural | value kind (date, email, integer, code…) and character-shape similarity |
| agreement | on rows that join (same entity), do the values agree? `(agree+1)/(n+2)`, so small samples can never reach 100% |
| overlap | distinct values the columns share (positive evidence only) |

Guard rails: contradicting values on ≥3 joined rows block a merge; inferred scores
are capped (0.90 with no data evidence, 0.985 at most); a runner-up within 0.10
subtracts up to 0.10; declared aliases are floored at 0.95. Columns that exist on one
side only get "how sure we are no counterpart exists" (`1 − best candidate score`),
and are flagged when a near-miss, a duplicate of an already-matched field, or a
same-named field with contradicting values exists. Thresholds
(`MERGE_THRESHOLD=0.70`, `CONFIDENCE_HIGH=0.85`, …) are constants at the top of
`field_matcher.py`.

**Connection-level numbers:** *schema overlap* = confidence-weighted share of fields
that have a counterpart (a soft Dice coefficient); *average match confidence* = mean
over matched columns; *needs review* = flagged columns.

## Project layout

```
backend/
  app/
    main.py            FastAPI app — all endpoints
    models_db.py        Mongo document factories: Organization, Dataset,
                        ConnectionRequest, Consent, AuditLog
    database.py         MongoDB connection setup (pymongo)
    auth.py              API-key auth dependency
    schemas_api.py       Pydantic request/response models
    xroad_adapter.py    Simulated X-Road secure exchange layer
    mapping_engine/     AI schema mapping (ported from the standalone
                        engine): schema discovery, LLM/fuzzy mapping,
                        transform runtime, conflict resolution
  tests/
    test_workflow.py    End-to-end integration test (register -> upload
                        -> request -> consent -> exchange -> audit)
  requirements.txt
  .env.example
frontend-src/                     React (Vite) source for the UI
  src/App.jsx, components/*.jsx   Screens: auth, overview, datasets, requests, admin review
  src/styles.css                  Light theme, Times New Roman
frontend/                         Built output of frontend-src, served by the backend at /app/
```

### Changing the UI
```bash
cd frontend-src
npm install
npm run dev      # hot-reload dev server on :5173, proxies /api to :8000
npm run build    # writes the production build into ../frontend
```
The built `frontend/` folder is already included, so just running the backend is enough to use the app.

## Running it

```bash
cd backend
pip install -r requirements.txt --break-system-packages   # or use a venv
cp .env.example .env    # fill in MONGO_URI (required), ADMIN_API_KEY, optionally MISTRAL_API_KEY

uvicorn app.main:app --reload --port 8000
```
Storage is MongoDB (`MONGO_URI` in `.env`) — a local `mongod` or an Atlas
cluster both work. There's no schema/migration step: collections and
indexes are created automatically on first run (see `init_db()` in
`app/database.py`).
Open **http://localhost:8000/app/** — register two organizations (open two
browser profiles / incognito windows to act as A and B) and a third window signed
in with your `ADMIN_API_KEY`. Add a dataset for each org, request a connection,
approve it as admin, consent as B, and pull the combined dataset.

API docs: http://localhost:8000/docs

Run the test suite:
```bash
python3 -m pytest tests/ -v
```
All tests pass without any API key or a live MongoDB (they force the
fuzzy mapping fallback for determinism, and use an in-memory `mongomock`
database instead of a real connection) — a Mistral key just makes the
mappings smarter, and a real `MONGO_URI` is only needed to actually run
the server.

## Where Mistral fits in

`mapping_engine/llm_mapper.py` is unchanged from the standalone engine:
set `MISTRAL_API_KEY` in `.env` and every similarity score and field
mapping is generated by Mistral instead of fuzzy name-matching. Nothing
else in the app changes. Try it without a key first — you'll see the fuzzy
fallback confidently get some mappings wrong (e.g. copying a whole
`full_name` string into both `first_name` and `last_name` instead of
splitting it) and correctly flag others as low-confidence; that's the
exact gap the real LLM closes.

## About the X-Road integration

There's no real X-Road Security Server wired in here — standing one up
(certs, subsystem registration, a Central Server) is real infrastructure
a pilot can't fake convincingly. `app/xroad_adapter.py` instead models the
*security properties* X-Road provides — signed handshakes, signed exchange
envelopes, a durable audit trail — behind a small interface. The file has
inline notes on exactly what to swap in once a real Security Server is
available: mutual subsystem/ACL verification in `secure_handshake()`, and
routing the actual `/exchange` HTTP call through your local Security
Server (which then handles signing and logging for you) instead of the
local HMAC scheme.

**Before any non-local/non-pilot use:** change `XROAD_SHARED_SECRET` in
`.env` to a real secret, and note the current auth model (one static API
key per org, no user accounts, no rate limiting) is intentionally minimal
— fine for a pilot with a handful of known organizations, not for exposing
on the open internet as-is.

## Design notes / what to extend next

- **Multiple datasets per org / picking which to enrich**: already
  supported — `GET /api/orgs/{id}/datasets` lists all of an org's datasets
  (schema only) so a requester can choose the right one.
- **Admin tooling**: the admin approves or rejects a whole connection. Natural
  next steps: let the admin override a single mapping (force-merge / split a
  column) before approving, and give the admin a real login with roles instead
  of one static key.
- **Embeddings calibration**: with `MISTRAL_API_KEY` set, name embeddings are
  blended into the semantic score; the rescaling window in `semantic.py`
  (`0.55-0.95`) is provisional - tune it against your own field pairs.
- **Size limits**: uploads are capped at 8 MB / 50,000 rows; MongoDB's 16 MB
  document limit applies to stored records.
- **Auth**: swap the static API-key scheme for per-user accounts within an
  org (roles: who can upload datasets vs. who can sign consent) before
  this goes beyond a pilot.
- **Notifications**: B currently has to check "Incoming Requests" — wire
  up real email/webhook notifications when a request lands or a decision
  is made.
- **Consent documents**: `agreement_text` is free text today; add real
  file upload (signed PDF) if a typed agreement isn't sufficient for your
  compliance needs.
