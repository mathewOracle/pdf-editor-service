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
