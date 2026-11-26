"""Local web server: upload a document, watch the run, read the report.

    uv run reqaudit-ui

The server starts the same console command a user would (`python -m reqaudit ...`) and
then only reads the run folder. Model keys stay in the server's environment (.env);
nothing about them is sent to the browser.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from reqaudit import __version__
from reqaudit.config import PROFILES, load_settings
from reqaudit.ui import rundata

_UPLOADS = "_uploads"
_START_TIMEOUT_S = 90.0
_BAD_NAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def _problem(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def create_app(runs_dir: Path | None = None) -> FastAPI:
    settings = load_settings(runs_dir=runs_dir)
    runs = settings.runs_dir
    app = FastAPI(title="reqaudit", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)
    processes: dict[str, subprocess.Popen] = {}
    lock = threading.Lock()

    def alive_map() -> dict[str, bool]:
        with lock:
            return {run_id: proc.poll() is None for run_id, proc in processes.items()}

    def run_dir(run_id: str) -> Path:
        # The id is checked against the pipeline's own format, so it cannot be a path.
        if not rundata.RUN_ID.match(run_id) or not (runs / run_id).is_dir():
            raise _problem(404, f"Прогон {run_id} не найден в папке {runs.as_posix()}/. "
                                "Возможно, папку прогонов очистили; запустите проверку заново.")
        return runs / run_id

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, err: HTTPException) -> JSONResponse:
        return JSONResponse({"error": err.detail}, status_code=err.status_code)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(rundata.page(), headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    def config() -> dict:
        # Re-read on every call: the user may add a key to .env while the server runs.
        current = load_settings(runs_dir=runs)
        return {
            "version": __version__,
            "profiles": list(PROFILES),
            "default_profile": current.profile,
            "max_input_mb": current.max_input_mb,
            "model_ready": bool(current.chain_for("judge")),
            "models": current.chain_for("judge"),
            "notes": current.notes,
            "runs_dir": runs.as_posix(),
        }

    @app.get("/api/runs")
    def list_runs() -> dict:
        return {"runs": rundata.list_runs(runs, alive_map())}

    @app.post("/api/runs")
    async def start_run(request: Request, name: str, profile: str = "quick") -> dict:
        if profile not in PROFILES:
            raise _problem(400, f"Неизвестный профиль «{profile}». Выберите quick или thorough.")
        file_name = _BAD_NAME.sub("_", Path(name).name).strip(" .") or "document"
        body = await request.body()
        if not body:
            raise _problem(400, "Файл пустой. Выберите документ с требованиями.")
        limit = load_settings(runs_dir=runs).max_input_mb
        if len(body) > limit * 1024 * 1024:
            raise _problem(413, f"Файл больше {limit} МБ. Уменьшите его или поднимите "
                                "REQAUDIT_MAX_INPUT_MB в файле .env.")

        # Starting the agent waits for its run folder; keep that off the event loop.
        return await asyncio.to_thread(launch, file_name, body, profile)

    def launch(file_name: str, body: bytes, profile: str) -> dict:
        token = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        upload = (runs / _UPLOADS / token).resolve()
        upload.mkdir(parents=True, exist_ok=True)
        source = upload / file_name
        source.write_bytes(body)
        log = upload / "console.log"

        known = {d.name for d in runs.iterdir() if d.is_dir()}
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        with log.open("wb") as out:
            proc = subprocess.Popen(  # noqa: S603 - fixed command, the file name is an argument
                [sys.executable, "-m", "reqaudit", str(source), str(upload / "report.md"),
                 "--profile", profile, "--runs-dir", str(runs)],
                stdout=out, stderr=subprocess.STDOUT, env=env)

        # The pipeline names the run folder itself; find it by the document it was given.
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            for d in runs.iterdir():
                if d.is_dir() and d.name not in known and rundata.RUN_ID.match(d.name):
                    events = rundata.read_trace(d)
                    if events and Path(str(events[0].get("input", ""))) == source:
                        with lock:
                            processes[d.name] = proc
                        return {"run_id": d.name}
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        tail = log.read_text(encoding="utf-8", errors="replace").strip()[-600:]
        if proc.poll() is None:
            proc.kill()
            raise _problem(500, "Агент не начал прогон за полторы минуты и был остановлен. "
                                "Запустите проверку ещё раз; если повторится, запустите "
                                "reqaudit из консоли и посмотрите вывод.")
        raise _problem(500, "Агент завершился, не начав прогон. Вывод консоли: "
                            + (tail or "пусто"))

    @app.get("/api/runs/{run_id}/progress")
    def run_progress(run_id: str) -> dict:
        return rundata.progress(run_dir(run_id), alive_map().get(run_id))

    @app.get("/api/runs/{run_id}/report")
    def run_report(run_id: str) -> dict:
        try:
            return rundata.build_report(run_dir(run_id))
        except FileNotFoundError:
            raise _problem(409, "У этого прогона ещё нет итогового состояния (state.json): "
                                "он не завершён или был прерван. Откройте ход прогона или "
                                "запустите проверку заново.") from None

    @app.get("/api/runs/{run_id}/report.md")
    def report_md(run_id: str) -> Response:
        path = run_dir(run_id) / "report.md"
        if not path.exists():
            raise _problem(404, "В папке прогона нет report.md: прогон не дошёл до отчёта.")
        return Response(path.read_bytes(), media_type="text/markdown; charset=utf-8",
                        headers=_attachment(f"{run_id}.report.md"))

    @app.get("/api/runs/{run_id}/report.html")
    def report_html(run_id: str) -> Response:
        try:
            html = rundata.render_html(rundata.build_report(run_dir(run_id)))
        except FileNotFoundError:
            raise _problem(409, "Прогон не завершён: report.html собирается из итогового "
                                "состояния, которого ещё нет.") from None
        return Response(html.encode("utf-8"), media_type="text/html; charset=utf-8",
                        headers=_attachment(f"{run_id}.report.html"))

    return app


def _attachment(file_name: str) -> dict[str, str]:
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reqaudit-ui",
        description="Веб-интерфейс агента проверки требований: загрузка документа, "
                    "ход прогона и отчёт. Работает локально и без интернета.")
    parser.add_argument("--host", default="127.0.0.1", help="адрес; по умолчанию 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="порт; по умолчанию 8000")
    parser.add_argument("--runs-dir", type=Path,
                        help="папка с прогонами; по умолчанию runs/")
    parser.add_argument("--no-browser", action="store_true",
                        help="не открывать страницу в браузере")
    args = parser.parse_args(argv)

    import uvicorn

    try:
        app = create_app(args.runs_dir)
    except ValueError as err:
        print(f"Ошибка настройки: {err}", file=sys.stderr)
        return 2
    url = f"http://{args.host}:{args.port}/"
    print(f"Интерфейс reqaudit: {url}")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
