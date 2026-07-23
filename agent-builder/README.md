# Agent Builder

Portable YAML generation toolkit — **separate from the Data Manager app**.
Move this entire folder to another repo or machine when needed.

## Layout

```text
agent-builder/
  core/                    # Shared: LiteLLM, Databricks, YAML utils, validate hook
  agents/
    table_config/          # Data Manager config/tables/*.yaml
      generate.py
      prompt.md
      example.yaml
  requirements.txt
  .env.example
```

Add new domains later as `agents/<name>/` (same `core/`, new prompt + CLI).

## Setup

```bash
cd agent-builder
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

| Variable | Purpose |
|----------|---------|
| `LITELLM_BASE_URL` | Gateway URL (`.../v1`) |
| `LITELLM_API_KEY` | API key |
| `LITELLM_MODEL` | e.g. `gpt-5` |
| `DATABRICKS_*` | Only if using `--fetch-describe` |
| `DATA_EDITOR_ROOT` | Path to data-editor repo (default: parent folder) |

Databricks credentials can come from `DATA_EDITOR_ROOT/.env` if not set here.

## Generate table YAML

**Auto DESCRIBE from Databricks:**

```bash
python agents/table_config/generate.py \
  --schema dmz \
  --table dash_test_carrier \
  --primary-keys carrierid \
  --fetch-describe \
  --validate
```

**From pasted DESCRIBE file:**

```bash
python agents/table_config/generate.py \
  --schema dmz \
  --table my_table \
  --primary-keys id \
  --describe-file input/describe.txt \
  --output ../config/tables/dmz.my_table.yaml \
  --validate
```

**Optional hints:**

```bash
--display-name "Carrier Master" \
--group General \
--hints "status is fixed dropdown ACTIVE,INACTIVE; state_code lookup dmz.ref_state_codes"
```

**Preview DESCRIBE prompt without calling LLM:**

```bash
python agents/table_config/generate.py ... --fetch-describe --dry-run-llm
```

## Ensure audit columns on prod tables

If a physical table is missing `version`, `inserted_by`, etc.:

```bash
# Show what would be added (safe — no changes)
python agents/table_config/ensure_audit_columns.py --schema dmz --table my_table

# Apply ALTER TABLE ADD COLUMN (+ backfill version=0 for existing rows)
python agents/table_config/ensure_audit_columns.py --schema dmz --table my_table --apply
```

**Process:** run `ensure_audit_columns` **before** generate + deploy for new tables.

- Uses `ALTER TABLE ADD COLUMN` only — does **not** recreate the table
- Skips columns already present (or legacy aliases like `created_by`)
- Backfills `version = 0` on existing rows when `version` is newly added

## After generation

1. Review YAML in Git
2. Deploy:

```bash
cd ..
python scripts/deploy_config.py --file config/tables/dmz.my_table.yaml
```

## Moving to another location

1. Copy the whole `agent-builder/` folder
2. Set `DATA_EDITOR_ROOT` to the Data Manager repo path
3. Or set `--output` to any path and skip `--validate`

## Security

- Never commit `.env` or API keys
- LLM prompts send **schema only** (DESCRIBE), not row data
