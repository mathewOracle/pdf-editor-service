"""FastAPI entrypoint: builds the app and plugs in the route modules."""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .fonts import warm_font_index
from .routes import documents, elements, undo
from .web import BASE_DIR

app = FastAPI(title="Offline PDF Editor")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
for module in (documents, elements, undo):
    app.include_router(module.router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    warm_font_index()
