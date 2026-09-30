# Database migrations

Numbered, forward-only SQL files applied in lexicographic order by

```bash
source .venv/bin/activate
python -m app.cli db-migrate            # apply everything not yet applied
python -m app.cli db-migrate --status   # show applied / pending versions
```

## Rules

1. **Never edit a released migration.** Add a new file instead. The applied
   version list lives in the `schema_migrations` table, so re-running an old
   file is a no-op, but editing one silently desynchronises existing installs.
2. **Additive first.** `ALTER TABLE ... ADD COLUMN`, `CREATE INDEX`,
   `CREATE VIEW`. SQLite cannot drop or alter a column in place; a table rebuild
   (create-copy-drop-rename inside a transaction) is the escape hatch and must
   be commented as such.
3. **Idempotent statements only** — `IF NOT EXISTS` / `INSERT OR IGNORE` — so a
   partially applied file can be re-run after fixing a typo.
4. **Each file records itself** with an `INSERT OR IGNORE INTO
   schema_migrations (version, description) VALUES (...)` at the end.
5. **No data loss.** Deleting personal data is an application-level,
   role-gated operation (`DELETE /api/students/{id}` deactivates by default),
   never a migration side effect.

## Adding a migration

```sql
-- database/migrations/0003_add_cashier_shifts.sql
ALTER TABLE transactions ADD COLUMN shift_label TEXT;
INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('0003', 'add shift_label to transactions');
```

Then mirror the column in `database/schema.sql` (the authoritative snapshot used
for fresh installs) and in `python/app/database/models.py` if it is exposed
through the API.
