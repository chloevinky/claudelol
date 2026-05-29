"""FastAPI app: REST endpoints, websocket push, static UI."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, data_dragon, db, distiller, logging_setup
from .claude_advisor import ClaudeAdvisor
from .game_monitor import GameMonitor

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class WSHub:
    """Tracks connected websockets and broadcasts JSON messages."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            stale: list[WebSocket] = []
            for ws in list(self._clients):
                try:
                    await ws.send_json(payload)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self._clients.discard(ws)


def _state_payload(monitor: GameMonitor, advisor: ClaudeAdvisor) -> dict[str, Any]:
    state = monitor.state
    fp = state.get("fingerprint")
    cached = advisor.cached(fp) if fp else None
    return {
        "in_game": bool(state.get("in_game")),
        "fingerprint": fp,
        "last_poll": state.get("last_poll", 0),
        "snapshot": state.get("snapshot"),
        "advice": (
            {
                "advice": cached.advice,
                "requested_at": cached.requested_at,
                "fingerprint": cached.fingerprint,
                "patch_version": cached.patch_version,
                "error": cached.error,
                "model": cached.model,
            }
            if cached
            else None
        ),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not logging.getLogger().handlers:
        logging_setup.setup()
    cfg = config.load()
    db.init(config.DB_PATH)
    log.info("LoL Live Coach starting. Logs dir: %s", logging_setup.LOGS_DIR)

    patch_watcher = data_dragon.PatchWatcher(
        db_path=config.DB_PATH,
        locale=cfg.get("ddragon_locale", "en_US"),
        interval_seconds=cfg.get("patch_check_interval_hours", 6) * 3600,
        get_config=config.load,
    )
    advisor = ClaudeAdvisor(get_config=config.load)
    hub = WSHub()

    async def on_state_change(state: dict[str, Any]) -> None:
        payload = _state_payload(monitor, advisor)
        await hub.broadcast({"type": "state", "data": payload})

        cfg_now = config.load()
        if (
            cfg_now.get("advisor_enabled")
            and cfg_now.get("anthropic_api_key")
            and state.get("in_game")
            and state.get("snapshot")
            and state.get("fingerprint")
        ):
            fp = state["fingerprint"]
            if advisor.cached(fp) is None:
                task = asyncio.create_task(
                    _request_and_broadcast(advisor, hub, state["snapshot"], fp, patch_watcher.last_version)
                )
                task.add_done_callback(_log_task_exception)

    monitor = GameMonitor(get_config=config.load, on_state_change=on_state_change)

    patch_watcher.start()
    monitor.start()

    # Distill the already-cached patch right away so the advisor has grounded
    # facts even before the watcher's first network check completes.
    cached_version = db.patch_summary(config.DB_PATH).get("version")
    if cached_version:
        startup_distill = asyncio.create_task(
            distiller.distill_patch(config.DB_PATH, cached_version, config.load),
            name=f"distill-startup-{cached_version}",
        )
        startup_distill.add_done_callback(_log_task_exception)

    app.state.config = config
    app.state.monitor = monitor
    app.state.advisor = advisor
    app.state.hub = hub
    app.state.patch_watcher = patch_watcher
    try:
        yield
    finally:
        await monitor.stop()
        await patch_watcher.stop()


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.exception("Background task failed", exc_info=exc)


async def _request_and_broadcast(
    advisor: ClaudeAdvisor,
    hub: WSHub,
    snapshot: dict[str, Any],
    fingerprint: str,
    patch_version: str | None,
) -> None:
    response = await advisor.get_advice(snapshot, fingerprint, patch_version)
    await hub.broadcast(
        {
            "type": "advice",
            "data": {
                "advice": response.advice,
                "requested_at": response.requested_at,
                "fingerprint": response.fingerprint,
                "patch_version": response.patch_version,
                "cached": response.cached,
                "error": response.error,
                "model": response.model,
            },
        }
    )


def create_app() -> FastAPI:
    app = FastAPI(title="LoL Live Stats AI", lifespan=lifespan)

    @app.middleware("http")
    async def _revalidate_static(request, call_next):
        """Serve the UI with must-revalidate so browsers never run stale assets.

        ``no-cache`` keeps the file cached but forces a conditional request on
        every load; StaticFiles answers with a cheap 304 when nothing changed,
        or the fresh file the moment we ship a UI update — so there's no need to
        version asset URLs by hand.
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return _state_payload(app.state.monitor, app.state.advisor)

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return config.public_view(config.load())

    @app.post("/api/config")
    async def update_config(payload: dict[str, Any]) -> dict[str, Any]:
        if "anthropic_api_key" in payload:
            key = payload["anthropic_api_key"]
            if not key or key.startswith("sk-..."):
                payload.pop("anthropic_api_key", None)
        merged = config.save(payload)
        return config.public_view(merged)

    @app.get("/api/patch")
    async def get_patch() -> dict[str, Any]:
        return db.patch_summary(config.DB_PATH)

    @app.post("/api/patch/refresh")
    async def refresh_patch() -> dict[str, Any]:
        cfg = config.load()
        return await data_dragon.ensure_current_patch(
            config.DB_PATH, cfg.get("ddragon_locale", "en_US"), force=True,
            get_config=config.load,
        )

    @app.get("/api/champions")
    async def list_champions() -> dict[str, Any]:
        version = db.patch_summary(config.DB_PATH).get("version")
        if not version:
            return {"version": None, "champions": []}
        return {
            "version": version,
            "champions": db.all_champion_names(config.DB_PATH, version),
        }

    @app.post("/api/pregame")
    async def pregame_advice(payload: dict[str, Any]) -> dict[str, Any]:
        champion = (payload.get("champion") or "").strip()
        role = (payload.get("role") or "").strip()
        topic = (payload.get("topic") or "runes").strip()
        if not champion:
            raise HTTPException(status_code=400, detail="A champion is required.")
        from .claude_advisor import PREGAME_TOPICS
        if topic not in PREGAME_TOPICS:
            raise HTTPException(status_code=400, detail=f"Unknown topic {topic!r}.")
        cfg = config.load()
        if not cfg.get("anthropic_api_key"):
            raise HTTPException(status_code=400, detail="Anthropic API key not configured.")
        version = app.state.patch_watcher.last_version or db.patch_summary(config.DB_PATH).get("version")
        resp = await app.state.advisor.get_pregame_advice(topic, champion, role, version)
        return {
            "topic": topic,
            "champion": champion,
            "role": role,
            "patch_version": resp.patch_version,
            "advice": resp.advice,
            "cached": resp.cached,
            "error": resp.error,
            "model": resp.model,
        }

    @app.get("/api/distill")
    async def get_distill() -> dict[str, Any]:
        version = db.patch_summary(config.DB_PATH).get("version")
        return await asyncio.to_thread(
            distiller.distill_status, config.DB_PATH, version, config.load
        )

    @app.post("/api/distill/run")
    async def run_distill() -> dict[str, Any]:
        version = db.patch_summary(config.DB_PATH).get("version")
        if not version:
            raise HTTPException(status_code=503, detail="Patch data not loaded yet.")
        cfg = config.load()
        if not cfg.get("anthropic_api_key"):
            raise HTTPException(status_code=400, detail="Anthropic API key not configured.")
        if not cfg.get("distiller_enabled", True):
            raise HTTPException(status_code=409, detail="Distiller is disabled in settings.")
        if distiller._distill_lock.locked():
            return {"started": False, "running": True, "version": version}
        task = asyncio.create_task(
            distiller.distill_patch(config.DB_PATH, version, config.load),
            name=f"distill-manual-{version}",
        )
        task.add_done_callback(_log_task_exception)
        return {"started": True, "running": True, "version": version}

    @app.post("/api/advice/refresh")
    async def refresh_advice() -> dict[str, Any]:
        state = app.state.monitor.state
        if not state.get("in_game") or not state.get("snapshot") or not state.get("fingerprint"):
            raise HTTPException(status_code=409, detail="Not currently in a game.")
        cfg = config.load()
        if not cfg.get("anthropic_api_key"):
            raise HTTPException(status_code=400, detail="Anthropic API key not configured.")
        app.state.advisor.clear_cache()
        patch_version = app.state.patch_watcher.last_version
        response = await app.state.advisor.get_advice(
            state["snapshot"], state["fingerprint"], patch_version
        )
        await app.state.hub.broadcast(
            {
                "type": "advice",
                "data": {
                    "advice": response.advice,
                    "requested_at": response.requested_at,
                    "fingerprint": response.fingerprint,
                    "patch_version": response.patch_version,
                    "cached": response.cached,
                    "error": response.error,
                    "model": response.model,
                },
            }
        )
        return {
            "advice": response.advice,
            "error": response.error,
            "model": response.model,
        }

    @app.get("/api/logs")
    async def list_logs() -> dict[str, Any]:
        logs_dir = logging_setup.LOGS_DIR
        files = []
        if logs_dir.exists():
            for f in sorted(logs_dir.iterdir()):
                if f.is_file():
                    stat = f.stat()
                    files.append(
                        {
                            "name": f.name,
                            "size_bytes": stat.st_size,
                            "modified_at": stat.st_mtime,
                        }
                    )
        return {"logs_dir": str(logs_dir), "files": files}

    @app.get("/api/logs/file/{name}")
    async def download_log(name: str) -> FileResponse:
        # Hard-block any path component — only files directly inside LOGS_DIR.
        if "/" in name or "\\" in name or ".." in name or name.startswith("."):
            raise HTTPException(status_code=400, detail="Invalid log filename.")
        path = logging_setup.LOGS_DIR / name
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail=f"No such log: {name}")
        # Resolve and verify the path is still inside LOGS_DIR (symlink safety).
        if logging_setup.LOGS_DIR.resolve() not in path.resolve().parents:
            raise HTTPException(status_code=400, detail="Invalid log path.")
        return FileResponse(path, media_type="text/plain", filename=name)

    @app.get("/api/logs/tail")
    async def tail_general(lines: int = 200) -> dict[str, Any]:
        path = logging_setup.GENERAL_LOG
        if not path.exists():
            return {"lines": []}
        lines = max(1, min(int(lines), 5000))
        with path.open("rb") as f:
            try:
                f.seek(0, 2)
                size = f.tell()
                # Read the last ~256KB which is more than enough for 5000 lines
                read_size = min(size, 256 * 1024)
                f.seek(size - read_size)
                tail = f.read().decode("utf-8", errors="replace")
            except OSError:
                tail = ""
        return {"lines": tail.splitlines()[-lines:]}

    @app.get("/api/champion/{name}")
    async def champion_lookup(name: str) -> dict[str, Any]:
        summary = db.patch_summary(config.DB_PATH)
        version = summary.get("version")
        if not version:
            raise HTTPException(status_code=503, detail="Patch data not loaded yet.")
        champ = db.champion_summary(config.DB_PATH, version, name)
        if not champ:
            raise HTTPException(status_code=404, detail=f"Unknown champion {name!r}.")
        return champ

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        await app.state.hub.add(ws)
        try:
            await ws.send_json(
                {"type": "state", "data": _state_payload(app.state.monitor, app.state.advisor)}
            )
            while True:
                msg = await ws.receive_text()
                if msg == "ping":
                    await ws.send_text("pong")
        except WebSocketDisconnect:
            pass
        finally:
            await app.state.hub.remove(ws)

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(Exception)
    async def unhandled(request, exc):
        log.exception("Unhandled error")
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return app


app = create_app()
