"""``photomigrate review``: the local review gallery (FastAPI, Jinja2, htmx). CLA-88.

It only **records decisions**: every answer is written to the catalogue's ``decisions`` table
at once, so a review can stop and resume at any time. Nothing else is changed. Importing,
staging "To Delete" and writing dates and places happen later, in ``import``.

Who can reach it: the server binds to ``127.0.0.1`` only (there is no option to change that),
requests naming any other host are refused (DNS rebinding), and requests that write must come
from the gallery's own pages (a same-origin ``Origin`` header), so another web site open in
the browser cannot record answers.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from photomigrate.analyse.date_fixes import AnswerError, fixes
from photomigrate.catalogue import Catalogue
from photomigrate.review.queues import KINDS, QUEUES, Card, Value, as_json, progress
from photomigrate.review.thumbs import Thumbnails
from photomigrate.review.tiles import Fetch, Tiles, fetch

__all__ = ["QUEUES", "create_app", "serve", "server_config"]

HOST = "127.0.0.1"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
PAGE_SIZE = 40
HERE = Path(__file__).parent
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return origin is not None and origin == f"http://{request.headers.get('host', '')}"


def create_app(
    catalogue_path: Path,
    *,
    thumbs: Path,
    takeout: Path | None = None,
    library: Path | None = None,
    fetch_tile: Fetch = fetch,
) -> FastAPI:
    app = FastAPI(title="photomigrate review", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["as_json"] = as_json
    thumbnails = Thumbnails(thumbs, takeout=takeout, library=library)
    thumbnail_lock = threading.Lock()  # zip readers are not thread-safe
    tiles = Tiles(thumbs / "tiles", fetch_tile)

    @app.middleware("http")
    async def writes_come_from_our_pages(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in _SAFE_METHODS and not _same_origin(request):
            return PlainTextResponse("decisions are only taken from the gallery's pages", 403)
        return await call_next(request)

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def open_catalogue() -> Catalogue:
        return Catalogue.open(catalogue_path)

    def card_of(catalogue: Catalogue, kind: str, subject_id: int) -> Card:
        for card in KINDS[kind].cards(catalogue):
            if card.subject_id == subject_id:
                return card
        raise HTTPException(404, f"no {kind} question {subject_id}")

    def render_card(request: Request, catalogue: Catalogue, kind: str, subject_id: int) -> Response:
        queue = KINDS[kind]
        card = card_of(catalogue, kind, subject_id)
        return templates.TemplateResponse(request, "_card.html", {"card": card, "queue": queue})

    def record(catalogue: Catalogue, subject: str, subject_id: int, kind: str, value: Any) -> None:
        """Check the answer fits a question the gallery shows, then store it."""
        if kind not in KINDS:
            raise HTTPException(400, f"no such question: {kind!r}")
        if subject != KINDS[kind].subject:
            raise HTTPException(400, f"a {kind} answer is about an {KINDS[kind].subject}")
        if not isinstance(value, dict):
            raise HTTPException(400, "an answer is a JSON object")
        card_of(catalogue, kind, subject_id)
        if kind == "date":
            try:
                fixes([], value, {})
            except AnswerError as exc:
                raise HTTPException(400, str(exc)) from None
        catalogue.set_decision(KINDS[kind].subject, subject_id, kind, value)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        with open_catalogue() as catalogue:
            rows = [(queue, *progress(queue.cards(catalogue))) for queue in QUEUES.values()]
        return templates.TemplateResponse(request, "index.html", {"rows": rows})

    @app.get("/queue/{name}", response_class=HTMLResponse)
    def queue_page(request: Request, name: str, page: int = 1) -> Response:
        if name not in QUEUES:
            raise HTTPException(404, f"no queue {name!r}")
        queue = QUEUES[name]
        with open_catalogue() as catalogue:
            cards = queue.cards(catalogue)
        decided, total = progress(cards)
        start = (max(page, 1) - 1) * PAGE_SIZE
        context = {
            "queue": queue,
            "cards": cards[start : start + PAGE_SIZE],
            "all_cards": cards,
            "decided": decided,
            "total": total,
            "page": max(page, 1),
            "pages": max(1, -(-total // PAGE_SIZE)),
        }
        return templates.TemplateResponse(request, "queue.html", context)

    @app.post("/decide", response_class=HTMLResponse)
    def decide(
        request: Request,
        subject: Annotated[str, Form()],
        subject_id: Annotated[int, Form()],
        kind: Annotated[str, Form()],
        value: Annotated[str, Form()],
    ) -> Response:
        try:
            answer = json.loads(value)
        except json.JSONDecodeError:
            raise HTTPException(400, "the answer is not JSON") from None
        with open_catalogue() as catalogue:
            record(catalogue, subject, subject_id, kind, answer)
            return render_card(request, catalogue, kind, subject_id)

    @app.post("/decide/date", response_class=HTMLResponse)
    def decide_date(
        request: Request,
        subject_id: Annotated[int, Form()],
        answer: Annotated[str, Form()],
        date: Annotated[str, Form()] = "",
        year: Annotated[str, Form()] = "",
        offset_hours: Annotated[str, Form()] = "",
    ) -> Response:
        value: Value = {"answer": answer}
        try:
            if answer == "date":
                value["date"] = date
            elif answer == "year":
                value["year"] = int(year)
            elif answer == "shift":
                value["offset_seconds"] = round(float(offset_hours) * 3600)
        except ValueError:
            raise HTTPException(400, f"{answer}: not a number") from None
        with open_catalogue() as catalogue:
            record(catalogue, "group", subject_id, "date", value)
            return render_card(request, catalogue, "date", subject_id)

    @app.post("/decide/best-shot", response_class=HTMLResponse)
    def decide_best_shot(
        request: Request,
        subject_id: Annotated[int, Form()],
        keep: Annotated[list[str] | None, Form()] = None,
        kind: Annotated[str, Form()] = "best-shot",
    ) -> Response:
        """Picks for a best-shot group, or for a year's event in the highlights (which may keep
        none of its photos)."""
        if kind not in ("best-shot", "highlights"):
            raise HTTPException(400, f"{kind}: not a group of picks")
        with open_catalogue() as catalogue:
            group = next((g for g in catalogue.groups(kind) if g.id == subject_id), None)  # type: ignore[arg-type]
            if group is None:
                raise HTTPException(404, f"no {kind} group {subject_id}")
            picks = keep or []
            if not set(picks) <= set(group.details["scores"]):
                raise HTTPException(400, "keep only the group's own shots")
            if not picks and kind == "best-shot":
                raise HTTPException(400, "keep at least one of the group's own shots")
            record(catalogue, "group", subject_id, kind, {"keep": picks})
            return render_card(request, catalogue, kind, subject_id)

    @app.post("/bulk")
    def bulk(
        subject: Annotated[str, Form()],
        kind: Annotated[str, Form()],
        ids: Annotated[str, Form()],
        value: Annotated[str, Form()],
    ) -> Response:
        try:
            answer = json.loads(value)
        except json.JSONDecodeError:
            raise HTTPException(400, "the answer is not JSON") from None
        targets = [int(i) for i in ids.split(",") if i]
        with open_catalogue() as catalogue, catalogue.transaction():
            for subject_id in targets:
                given = answer
                if kind == "edit-pair" and isinstance(answer, dict) and "policy" in answer:
                    given = _policy_keep(catalogue, subject_id, str(answer["policy"]))
                record(catalogue, subject, subject_id, kind, given)
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    @app.get("/thumb/{item_id}")
    def thumb(item_id: int) -> Response:
        with open_catalogue() as catalogue:
            try:
                item = catalogue.item(item_id)
            except KeyError:
                raise HTTPException(404, f"no item {item_id}") from None
        with thumbnail_lock:
            data = thumbnails.get(item)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/tile/{zoom}/{x}/{y}.png")
    def tile(zoom: int, x: int, y: int) -> Response:
        try:
            data = tiles.get(zoom, x, y)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        except OSError as exc:
            raise HTTPException(502, f"the map tile could not be fetched: {exc}") from None
        return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=604800"})

    return app


def _policy_keep(catalogue: Catalogue, group_id: int, policy: str) -> Value:
    members = [catalogue.item(i).source_key for i, _, _ in catalogue.group_members(group_id)]
    keep = {"pair": members, "original-only": members[:1], "edited-only": members[1:]}
    if policy not in keep:
        raise HTTPException(400, f"no edit policy {policy!r}")
    return {"keep": keep[policy]}


def server_config(app: FastAPI, port: int) -> uvicorn.Config:
    """Always 127.0.0.1: the gallery is for this Mac only."""
    return uvicorn.Config(app, host=HOST, port=port, log_level="warning")


def serve(app: FastAPI, port: int) -> None:
    uvicorn.Server(server_config(app, port)).run()
