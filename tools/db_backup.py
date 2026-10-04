"""
Back up and restore the Legal Document Summarizer database.

Works with any database the app supports (local SQLite, Render Postgres,
Neon Postgres) because it goes through the app's own models. Needs only the
project's normal requirements; no PostgreSQL client tools.

Usage (run from the project folder, with the venv active):

  # Row counts, to check you are pointed at the right database
  python tools/db_backup.py counts --database-url "postgresql://..."

  # Copy everything out to a JSON file
  python tools/db_backup.py export backup.json --database-url "postgresql://...render..."

  # Load that file into a new, empty database
  python tools/db_backup.py import backup.json --database-url "postgresql://...neon..."

If --database-url is omitted, DATABASE_URL from the environment / .env is
used, and without that, the local legal.db.

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
from datetime import datetime

PROJECT_FOLDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Parents before children, so foreign keys are always satisfied on import
TABLE_ORDER = ["User", "UserPreference", "Document", "DocumentAnalysis", "ExtractedClause"]


def load_app(database_url):
    """Import the Flask app pointed at the chosen database. Must happen before
    'import app', because app.py reads DATABASE_URL when it is imported."""
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("SECRET_KEY", "backup-tool")
    sys.path.insert(0, PROJECT_FOLDER)
    import app as application_module  # noqa: E402  (creates any missing tables)
    return application_module


def describe_target(application_module):
    engine_url = application_module.database.engine.url
    return f"{engine_url.get_backend_name()} database '{engine_url.database}' on {engine_url.host or 'this computer'}"


def model_classes(application_module):
    return [getattr(application_module, name) for name in TABLE_ORDER]


def row_to_dict(model_instance):
    row = {}
    for column in model_instance.__table__.columns:
        value = getattr(model_instance, column.name)
        row[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return row


def dict_to_row(model_class, row_data):
    values = {}
    for column in model_class.__table__.columns:
        value = row_data.get(column.name)
        if value is not None and column.type.python_type is datetime:
            value = datetime.fromisoformat(value)
        values[column.name] = value
    return model_class(**values)


def count_rows(application_module):
    return {model_class.__tablename__: application_module.database.session.query(model_class).count()
            for model_class in model_classes(application_module)}


def command_counts(application_module, arguments):
    print(f"Connected to: {describe_target(application_module)}")
    for table_name, row_count in count_rows(application_module).items():
        print(f"  {table_name:<20} {row_count:>6} rows")


def command_export(application_module, arguments):
    print(f"Exporting from: {describe_target(application_module)}")
    backup = {
        "format": "legal-doc-summarizer-backup-v1",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "tables": {},
    }
    for model_class in model_classes(application_module):
        rows = [row_to_dict(instance) for instance in
                application_module.database.session.query(model_class).order_by(model_class.id).all()]
        backup["tables"][model_class.__tablename__] = rows
        print(f"  {model_class.__tablename__:<20} {len(rows):>6} rows")

    with open(arguments.backup_file, "w", encoding="utf-8") as backup_handle:
        json.dump(backup, backup_handle, ensure_ascii=False)
    size_kilobytes = os.path.getsize(arguments.backup_file) / 1024
    print(f"Saved {arguments.backup_file} ({size_kilobytes:,.0f} KB). Keep this file somewhere safe.")


def reset_postgres_id_counters(application_module):
    """Rows were inserted with their original ids, which does not advance
    Postgres's auto-increment counters. Without this, the next new user or
    upload would try id=1 again and fail with a duplicate-key error."""
    connection = application_module.database.session.connection()
    for model_class in model_classes(application_module):
        quoted_table = f'"{model_class.__tablename__}"'
        connection.exec_driver_sql(
            f"SELECT setval(pg_get_serial_sequence('{quoted_table}', 'id'), "
            f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {quoted_table}"
        )


def command_import(application_module, arguments):
    database = application_module.database
    print(f"Importing into: {describe_target(application_module)}")

    with open(arguments.backup_file, encoding="utf-8") as backup_handle:
        backup = json.load(backup_handle)
    if backup.get("format") != "legal-doc-summarizer-backup-v1":
        sys.exit("This file is not a backup made by this tool.")

    existing_counts = count_rows(application_module)
    if any(existing_counts.values()):
        if not arguments.replace:
            sys.exit(
                f"The target database already has data {existing_counts}.\n"
                "Nothing was changed. Use --replace to delete it and load the backup instead."
            )
        print("  --replace: deleting existing rows in the target first")
        for model_class in reversed(model_classes(application_module)):
            database.session.query(model_class).delete()

    try:
        for model_class in model_classes(application_module):
            rows = backup["tables"].get(model_class.__tablename__, [])
            database.session.add_all(dict_to_row(model_class, row_data) for row_data in rows)
            database.session.flush()  # insert parents before children
            print(f"  {model_class.__tablename__:<20} {len(rows):>6} rows loaded")
        if database.engine.dialect.name == "postgresql":
            reset_postgres_id_counters(application_module)
        database.session.commit()
    except Exception:
        database.session.rollback()
        print("Import failed; the target database was left unchanged.")
        raise

    print("Done. Row counts now:", count_rows(application_module))


def main():
    parser = argparse.ArgumentParser(description="Back up or restore the app database.")
    parser.add_argument("command", choices=["counts", "export", "import"])
    parser.add_argument("backup_file", nargs="?", help="JSON file to write (export) or read (import)")
    parser.add_argument("--database-url", help="Overrides DATABASE_URL for this run")
    parser.add_argument("--replace", action="store_true", help="import: wipe the target's rows first")
    arguments = parser.parse_args()

    if arguments.command in ("export", "import") and not arguments.backup_file:
        parser.error(f"'{arguments.command}' needs a backup file name")

    application_module = load_app(arguments.database_url)
    with application_module.app.app_context():
        {"counts": command_counts, "export": command_export, "import": command_import}[arguments.command](
            application_module, arguments
        )


if __name__ == "__main__":
    main()
