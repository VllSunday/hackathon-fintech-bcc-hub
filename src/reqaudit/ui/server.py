"""Local web server: upload a document, watch the run, read the report.

    uv run reqaudit-ui

The server starts the same console command a user would (`python -m reqaudit ...`) and
then only reads the run folder. Model keys stay in the server's environment (.env);
nothing about them is sent to the browser.

A run spends the user's model budget, so the server only answers its own page: requests
must name this machine as their host (no DNS rebinding), an upload must come as
`application/octet-stream` from the same origin (a foreign site cannot send that without
a CORS preflight, which this server never grants), one run goes at a time, and the runs
the server started stop with it.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
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
from reqaudit.answers import fingerprint, render_template
from reqaudit.config import PROFILES, load_settings
from reqaudit.models import RunState
from reqaudit.ui import rundata

_UPLOADS = "_uploads"
_START_TIMEOUT_S = 90.0
_BAD_NAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_UPLOAD_TYPE = "application/octet-stream"
_ANSWERS_TYPE = "application/json"
_MAX_ANSWER_CHARS = 4000


def _problem(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def _host_name(value: str) -> str:
    """Host header without the port: «127.0.0.1:8000» → «127.0.0.1», «[::1]:80» → «::1»."""
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def create_app(runs_dir: Path | None = None,
               allowed_hosts: frozenset[str] | None = LOOPBACK) -> FastAPI:
    """The web app. `allowed_hosts=None` turns the host check off (`--allow-network`)."""
    settings = load_settings(runs_dir=runs_dir)
    runs = settings.runs_dir
    app = FastAPI(title="reqaudit", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)
    processes: dict[str, subprocess.Popen] = {}
    lock = threading.Lock()

    def alive_map() -> dict[str, bool]:
        with lock:
            return {run_id: proc.poll() is None for run_id, proc in processes.items()}

    def stop_all() -> None:
        """Runs started here stop with the server: they would go on spending the key."""
        with lock:
            for proc in processes.values():
                if proc.poll() is None:
                    proc.terminate()
            processes.clear()

    app.router.on_shutdown.append(stop_all)
    atexit.register(stop_all)

    @app.middleware("http")
    async def _only_this_machine(request: Request, call_next):
        host = _host_name(request.headers.get("host", ""))
        if allowed_hosts is not None and host not in allowed_hosts:
            return JSONResponse({"error": "Интерфейс отвечает только на адрес этого "
                                          "компьютера (127.0.0.1 или localhost)."},
                                status_code=421)
        return await call_next(request)

    def run_dir(run_id: str) -> Path:
        # The id is checked against the pipeline's own format, so it cannot be a path.
        if not rundata.RUN_ID.fullmatch(run_id) or not (runs / run_id).is_dir():
            raise _problem(404, f"Прогон {run_id} не найден в папке {runs.as_posix()}/. "
                                "Возможно, папку прогонов очистили; запустите проверку заново.")
        return runs / run_id

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, err: HTTPException) -> JSONResponse:
        return JSONResponse({"error": err.detail}, status_code=err.status_code)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(rundata.page(), headers={"Cache-Control": "no-store"})

    @app.get("/fonts/{name}")
    def font(name: str) -> Response:
        # Only the files shipped next to the page; the name pattern keeps it out of paths.
        path = rundata.font_path(name)
        if path is None:
            raise _problem(404, "Такого шрифта нет.")
        return Response(path.read_bytes(), media_type="font/woff2",
                        headers={"Cache-Control": "max-age=86400"})

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

    def own_page_only(request: Request, expected_type: str) -> None:
        """Only this page may start a run: a foreign site cannot send this content type
        without a CORS preflight, which the server never grants."""
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        origin = request.headers.get("origin")
        if content_type != expected_type or (
                origin and _host_name(origin.split("://", 1)[-1]) !=
                _host_name(request.headers.get("host", ""))):
            raise _problem(403, "Запрос принимается только со страницы этого интерфейса.")

    def one_run_at_a_time() -> None:
        if any(alive_map().values()):
            raise _problem(409, "Уже идёт проверка другого документа. Дождитесь её окончания: "
                                "один прогон за раз, чтобы не тратить ключ модели параллельно.")

    @app.post("/api/runs")
    async def start_run(request: Request, name: str, profile: str = "quick") -> dict:
        if profile not in PROFILES:
            raise _problem(400, f"Неизвестный профиль «{profile}». Выберите quick или thorough.")
        own_page_only(request, _UPLOAD_TYPE)
        one_run_at_a_time()
        limit = load_settings(runs_dir=runs).max_input_mb
        too_big = (f"Файл больше {limit} МБ. Уменьшите его или поднимите "
                   "REQAUDIT_MAX_INPUT_MB в файле .env.")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit * 1024 * 1024:
            raise _problem(413, too_big)
        file_name = _BAD_NAME.sub("_", Path(name).name).strip(" .") or "document"
        body = await request.body()
        if not body:
            raise _problem(400, "Файл пустой. Выберите документ с требованиями.")
        if len(body) > limit * 1024 * 1024:
            raise _problem(413, too_big)

        # Starting the agent waits for its run folder; keep that off the event loop.
        return await asyncio.to_thread(launch, file_name, body, profile)

    def work_dir() -> Path:
        token = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        folder = (runs / _UPLOADS / token).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def launch(file_name: str, body: bytes, profile: str) -> dict:
        upload = work_dir()
        source = upload / file_name
        source.write_bytes(body)
        return start(source, upload, ["--profile", profile])

    def start(source: Path, upload: Path, extra: list[str]) -> dict:
        log = upload / "console.log"
        known = {d.name for d in runs.iterdir() if d.is_dir()}
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        with log.open("wb") as out:
            proc = subprocess.Popen(  # noqa: S603 - fixed command, paths are arguments
                [sys.executable, "-m", "reqaudit", str(source), str(upload / "report.md"),
                 *extra, "--runs-dir", str(runs)],
                stdout=out, stderr=subprocess.STDOUT, env=env)

        # The pipeline names the run folder itself; find it by the document it was given.
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            for d in runs.iterdir():
                if d.is_dir() and d.name not in known and rundata.RUN_ID.fullmatch(d.name):
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

    @app.post("/api/runs/{run_id}/answers")
    async def answer_questions(run_id: str, request: Request) -> dict:
        """The author's answers: written to an answers file, then the same document is
        re-checked with `reqaudit --answers`; unchanged stages come from the cache."""
        own_page_only(request, _ANSWERS_TYPE)
        one_run_at_a_time()
        folder = run_dir(run_id)
        try:
            payload = await request.json()
        except ValueError:
            raise _problem(400, "Ответы не прочитаны: ожидался JSON.") from None
        given = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(given, dict):
            raise _problem(400, "Ответы не прочитаны: ожидалось поле answers.")
        state_path = folder / "state.json"
        if not state_path.exists():
            raise _problem(409, "Прогон не завершён: отвечать пока не на что.")
        state = RunState.model_validate_json(state_path.read_text(encoding="utf-8"))
        known = {q.id: q for q in state.questions}
        answers = {fingerprint(known[qid].text): " ".join(str(text).split())[:_MAX_ANSWER_CHARS]
                   for qid, text in given.items() if qid in known and str(text).strip()}
        answers |= {fingerprint(q.text): q.answer for q in state.questions
                    if q.answer and fingerprint(q.text) not in answers}
        if not answers:
            raise _problem(400, "Впишите ответ хотя бы на один вопрос.")
        source = Path(state.intake.path) if state.intake else None
        if source is None or not source.is_file():
            raise _problem(409, "Исходный документ этого прогона не найден на диске; загрузите "
                                "его заново и ответьте на вопросы нового прогона.")
        folder_for_answers = work_dir()
        answers_file = folder_for_answers / "answers.md"
        answers_file.write_text(render_template(state.questions, source.name, answers),
                                encoding="utf-8")
        passport = folder / "passport.json"
        profile = (json.loads(passport.read_text(encoding="utf-8")).get("profile")
                   if passport.exists() else None)
        if profile not in PROFILES:
            profile = load_settings(runs_dir=runs).profile
        return await asyncio.to_thread(start, source, folder_for_answers,
                                       ["--profile", profile, "--answers", str(answers_file)])

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
    parser.add_argument("--allow-network", action="store_true",
                        help="разрешить адрес, видимый из сети (--host 0.0.0.0): любой в сети "
                             "сможет загружать документы за ваш ключ модели и читать отчёты")
    args = parser.parse_args(argv)

    local = args.host in LOOPBACK
    if not local and not args.allow_network:
        print(f"Адрес {args.host} виден из сети: кто угодно в ней сможет запускать проверки "
              "за ваш ключ модели и читать отчёты. Если это нужно, добавьте --allow-network.",
              file=sys.stderr)
        return 2

    import uvicorn

    try:
        app = create_app(args.runs_dir, LOOPBACK if local else None)
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
