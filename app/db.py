"""Database engine + session helper + on-disk storage layout."""
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

# Uploaded PDFs/images and the SQLite DB get frequent small writes and need
# real file locking. Cloud-sync folders (OneDrive, iCloud Drive, Dropbox)
# use placeholder files and can silently make SQLite see a read-only
# filesystem, so runtime storage deliberately lives outside anything that
# might be synced -- NOT under this project's own directory on disk.
STORAGE_DIR = Path.home() / ".local" / "share" / "pdf-editor-service"
DB_PATH = STORAGE_DIR / "pdf_editor.db"

STORAGE_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_add_missing_columns()


def _migrate_add_missing_columns() -> None:
    """create_all() only creates tables that don't exist yet -- it never
    alters an existing table to add a newly-introduced column. Rather than
    wipe the sqlite file every time a model gains a field (which would
    destroy real work someone already did), check for each column this
    app has ever added after the initial release and ALTER TABLE it in if
    missing. Cheap and boring on purpose: no migration framework needed
    for a handful of nullable columns in a single-file local app.
    """
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(element)")}
        for column in (
            "redact_x0_frac",
            "redact_y0_frac",
            "redact_x1_frac",
            "redact_y1_frac",
        ):
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE element ADD COLUMN {column} REAL")
        conn.commit()


def get_session():
    with Session(engine) as session:
        yield session


def document_dir(document_id: int) -> Path:
    d = STORAGE_DIR / "documents" / str(document_id)
    (d / "assets").mkdir(parents=True, exist_ok=True)
    return d


def exports_dir() -> Path:
    d = STORAGE_DIR / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d
