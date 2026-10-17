"""
Back up and restore the Chambua database.

(The backup format identifier below still says "legal-doc-summarizer" on
purpose: changing it would make existing backup files unreadable.)

Works with any database the app supports (local SQLite, Render Postgres,
Neon Postgres). Needs only the project's normal requirements; no PostgreSQL
client tools.

Usage (run from the project folder, with the venv active):

  # Row counts, to check you are pointed at the right database
  python tools/db_backup.py counts --database-url "postgresql://..."

  # Copy everything out to a JSON file
  python tools/db_backup.py export backup.json --database-url "postgresql://...render..."

  # Load that file into a new, empty database
  python tools/db_backup.py import backup.json --database-url "postgresql://...neon..."

If --database-url is omitted, DATABASE_URL from the environment / .env is
used, and without that, the local legal.db.

Since increment 7 the tool reads tables straight from the database instead of
through the app's models, so:
  * counts and export never change the database, and work on a database from
    before or after increment 7;
  * import first brings the target up to the latest migration (creating the
    tables, roles and permissions), then loads the rows. A backup taken before
    increment 7 loads fine: every account becomes a Member, as on upgrade.

Roles and permissions are not copied from the backup: the target gets them
from the migrations, and each account's role is matched by name.

The import refuses to write into a database that already has data, unless
you add --replace (which deletes the target's existing rows first).

Uploaded files (uploads/) are NOT in the database and are not copied. Since
increment 3 the extracted text of each new document is stored in the
database, so summaries, clauses and the text survive a move.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

PROJECT_FOLDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_FORMAT = "legal-doc-summarizer-backup-v1"

# Parents before children, so foreign keys are always satisfied on import
DATA_TABLES = ["user", "user_preference", "document", "document_analysis", "extracted_clause", "audit_log"]
# Filled in by the migrations themselves; never counted as "existing data"
POLICY_TABLES = ["role", "permission", "role_permission"]


def load_app(database_url, run_migrations):
    """Import the Flask app pointed at the chosen database. Must happen before
    'import app', because app.py reads its settings when it is imported.
    With run_migrations=False the database is left exactly as it is."""
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("SECRET_KEY", "backup-tool")
    os.environ["AUTO_MIGRATE"] = "1" if run_migrations else "0"
    sys.path.insert(0, PROJECT_FOLDER)
    import app as application_module  # noqa: E402
    return application_module


def describe_target(engine):
    engine_url = engine.url
    return f"{engine_url.get_backend_name()} database '{engine_url.database}' on {engine_url.host or 'this computer'}"


def reflect_tables(engine):
    """The tables that really exist in this database, as they are."""
    import sqlalchemy
    metadata = sqlalchemy.MetaData()
    metadata.reflect(bind=engine)
    return metadata.tables


def to_json_value(value):
    return value.isoformat() if isinstance(value, datetime) else value


def from_json_value(column, value):
    import sqlalchemy
    if value is not None and isinstance(column.type, sqlalchemy.DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def count_rows(engine, table_names):
    import sqlalchemy
    tables = reflect_tables(engine)
    with engine.connect() as connection:
        return {
            table_name: connection.execute(sqlalchemy.select(sqlalchemy.func.count()).select_from(tables[table_name])).scalar()
            for table_name in table_names if table_name in tables
        }


def command_counts(engine, arguments):
    print(f"Connected to: {describe_target(engine)}")
    for table_name, row_count in count_rows(engine, DATA_TABLES + POLICY_TABLES).items():
        print(f"  {table_name:<20} {row_count:>6} rows")


def command_export(engine, arguments):
    import sqlalchemy
    print(f"Exporting from: {describe_target(engine)}")
    tables = reflect_tables(engine)
    backup = {
        "format": BACKUP_FORMAT,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "tables": {},
    }
    with engine.connect() as connection:
        for table_name in DATA_TABLES + POLICY_TABLES:
            if table_name not in tables:
                continue  # e.g. a database from before increment 7 has no audit_log
            table = tables[table_name]
            ordering = list(table.primary_key.columns) or list(table.columns)
            rows = [
                {key: to_json_value(value) for key, value in row._mapping.items()}
                for row in connection.execute(sqlalchemy.select(table).order_by(*ordering))
            ]
            backup["tables"][table_name] = rows
            print(f"  {table_name:<20} {len(rows):>6} rows")

    with open(arguments.backup_file, "w", encoding="utf-8") as backup_handle:
        json.dump(backup, backup_handle, ensure_ascii=False)
    size_kilobytes = os.path.getsize(arguments.backup_file) / 1024
    print(f"Saved {arguments.backup_file} ({size_kilobytes:,.0f} KB). Keep this file somewhere safe.")


def reset_postgres_id_counters(connection, tables, table_names):
    """Rows were inserted with their original ids, which does not advance
    Postgres's auto-increment counters. Without this, the next new user or
    upload would try id=1 again and fail with a duplicate-key error."""
    for table_name in table_names:
        if "id" not in tables[table_name].columns:
            continue
        quoted_table = f'"{table_name}"'
        connection.exec_driver_sql(
            f"SELECT setval(pg_get_serial_sequence('{quoted_table}', 'id'), "
            f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {quoted_table}"
        )


def role_ids_for_backup_users(backup, target_role_ids_by_name):
    """Maps each backup role id to the target's role id, by role name. A backup
    from before increment 7 has no roles: everyone becomes a Member."""
    backup_role_names_by_id = {row["id"]: row["name"] for row in backup["tables"].get("role", [])}
    member_role_id = target_role_ids_by_name["member"]

    def target_role_id(backup_role_id):
        return target_role_ids_by_name.get(backup_role_names_by_id.get(backup_role_id), member_role_id)
    return target_role_id


def command_import(engine, arguments):
    import sqlalchemy
    print(f"Importing into: {describe_target(engine)}")

    with open(arguments.backup_file, encoding="utf-8") as backup_handle:
        backup = json.load(backup_handle)
    if backup.get("format") != BACKUP_FORMAT:
        sys.exit("This file is not a backup made by this tool.")

    tables = reflect_tables(engine)
    existing_counts = count_rows(engine, DATA_TABLES)
    if any(existing_counts.values()) and not arguments.replace:
        sys.exit(
            f"The target database already has data {existing_counts}.\n"
            "Nothing was changed. Use --replace to delete it and load the backup instead."
        )

    with engine.connect() as connection:
        target_role_ids_by_name = dict(connection.execute(
            sqlalchemy.select(tables["role"].c.name, tables["role"].c.id)).all())
    target_role_id = role_ids_for_backup_users(backup, target_role_ids_by_name)

    try:
        with engine.begin() as connection:  # one transaction: all or nothing
            if any(existing_counts.values()):
                print("  --replace: deleting existing rows in the target first")
                for table_name in reversed(DATA_TABLES):
                    connection.execute(tables[table_name].delete())

            for table_name in DATA_TABLES:
                table = tables[table_name]
                rows = []
                for row_data in backup["tables"].get(table_name, []):
                    # Only columns the target has; columns the backup lacks get the
                    # database default (e.g. is_active, sidebar_collapsed)
                    row = {column.name: from_json_value(column, row_data[column.name])
                           for column in table.columns if column.name in row_data}
                    if table_name == "user":
                        row["role_id"] = target_role_id(row_data.get("role_id"))
                    rows.append(row)
                if rows:
                    connection.execute(table.insert(), rows)
                print(f"  {table_name:<20} {len(rows):>6} rows loaded")
            if engine.dialect.name == "postgresql":
                reset_postgres_id_counters(connection, tables, DATA_TABLES)
    except Exception:
        print("Import failed; the target database was left unchanged.")
        raise

    print("Done. Row counts now:", count_rows(engine, DATA_TABLES))


def main():
    parser = argparse.ArgumentParser(description="Back up or restore the app database.")
    parser.add_argument("command", choices=["counts", "export", "import"])
    parser.add_argument("backup_file", nargs="?", help="JSON file to write (export) or read (import)")
    parser.add_argument("--database-url", help="Overrides DATABASE_URL for this run")
    parser.add_argument("--replace", action="store_true", help="import: wipe the target's rows first")
    arguments = parser.parse_args()

    if arguments.command in ("export", "import") and not arguments.backup_file:
        parser.error(f"'{arguments.command}' needs a backup file name")

    # Only import changes the database (it needs the tables to exist first)
    application_module = load_app(arguments.database_url, run_migrations=arguments.command == "import")
    with application_module.app.app_context():
        engine = application_module.database.engine
        {"counts": command_counts, "export": command_export, "import": command_import}[arguments.command](
            engine, arguments
        )


if __name__ == "__main__":
    main()
