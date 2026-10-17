"""Alembic environment for Chambua (based on the Flask-Migrate template).

Changes from the template, each needed for this project:
  1. Logging is configured without disabling existing loggers. Migrations run
     at app start-up, and the template's default would silence the app's and
     gunicorn's logs on Render for the rest of the process.
  2. On SQLite, foreign-key enforcement is switched off while migrations run
     and back on afterwards. SQLite cannot alter a column in place, so Alembic
     rebuilds the table (copy, drop, rename); with enforcement on, dropping the
     'user' table would be refused because documents point at it.
     PostgreSQL alters columns in place and is not affected.
  3. render_as_batch on SQLite, which is what makes Alembic use that rebuild.
"""
import logging
from logging.config import fileConfig

from flask import current_app

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
logger = logging.getLogger('alembic.env')


def get_engine():
    try:
        # this works with Flask-SQLAlchemy<3 and Alchemical
        return current_app.extensions['migrate'].db.get_engine()
    except (TypeError, AttributeError):
        # this works with Flask-SQLAlchemy>=3
        return current_app.extensions['migrate'].db.engine


def get_engine_url():
    try:
        return get_engine().url.render_as_string(hide_password=False).replace(
            '%', '%%')
    except AttributeError:
        return str(get_engine().url).replace('%', '%%')


config.set_main_option('sqlalchemy.url', get_engine_url())
target_db = current_app.extensions['migrate'].db


def get_metadata():
    if hasattr(target_db, 'metadatas'):
        return target_db.metadatas[None]
    return target_db.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode (writes SQL instead of running it)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=get_metadata(), literal_binds=True,
        render_as_batch=url.startswith("sqlite"),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode, against the app's database."""

    # Prevents an empty migration file being generated when nothing changed
    # (reference: http://alembic.zzzcomputing.com/en/latest/cookbook.html)
    def process_revision_directives(context, revision, directives):
        if getattr(config.cmd_opts, 'autogenerate', False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info('No changes in schema detected.')

    conf_args = dict(current_app.extensions['migrate'].configure_args)
    if conf_args.get("process_revision_directives") is None:
        conf_args["process_revision_directives"] = process_revision_directives

    connectable = get_engine()
    is_sqlite = connectable.dialect.name == "sqlite"
    conf_args.setdefault("render_as_batch", is_sqlite)

    with connectable.connect() as connection:
        if is_sqlite:
            # Must run outside a transaction, or SQLite ignores it
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=get_metadata(),
                **conf_args
            )

            with context.begin_transaction():
                context.run_migrations()
        finally:
            if is_sqlite:
                # The connection goes back to the pool; restore enforcement
                connection.commit()
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
