from pathlib import Path


def apply_migrations(cursor) -> None:
    migration_dir = Path(__file__).resolve().parents[1] / "research" / "migrations"
    migrations = sorted(migration_dir.glob("[0-9][0-9][0-9]_*.sql"))
    if not migrations:
        raise FileNotFoundError(f"No SQL migrations found in {migration_dir}")
    for migration in migrations:
        version = int(migration.name[:3])
        cursor.execute("SELECT to_regclass('schema_migrations')")
        if cursor.fetchone()[0] is not None:
            cursor.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
            )
            if cursor.fetchone() is not None:
                continue
        cursor.execute(migration.read_text(encoding="utf-8"))
