from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def _add_column(engine: Engine, table: str, column: str, definition: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))


def apply_startup_migrations(engine: Engine) -> None:
    """Additive migrations for databases created by an earlier version.

    ``Base.metadata.create_all`` never adds columns to an existing table, so new
    columns are applied here instead of requiring a migration tool.
    """
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "users" in tables:
        columns = {column["name"] for column in inspector.get_columns("users")}
        if "failed_attempts" not in columns:
            _add_column(engine, "users", "failed_attempts", "INTEGER DEFAULT 0")
        if "locked_until" not in columns:
            _add_column(engine, "users", "locked_until", "DATETIME")

    if "alerts" in tables:
        columns = {column["name"] for column in inspector.get_columns("alerts")}
        if "host" not in columns:
            _add_column(engine, "alerts", "host", "VARCHAR(80) DEFAULT 'unassigned'")
            with engine.begin() as connection:
                connection.execute(text("UPDATE alerts SET host = 'unassigned' WHERE host IS NULL"))

    if "alert_rules" in tables:
        columns = {column["name"] for column in inspector.get_columns("alert_rules")}
        if "host" not in columns:
            _add_column(engine, "alert_rules", "host", "VARCHAR(80) DEFAULT ''")
