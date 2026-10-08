"""One HTTP process for MCP and the local read-only browser interface."""

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import anyio
from jinja2 import Environment, FileSystemLoader, select_autoescape
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from setauket.config import Config
from setauket.models import LocalModels
from setauket.storage import Store
from setauket.worker import Worker

log = logging.getLogger(__name__)
TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape())
TEMPLATES.filters["date"] = lambda timestamp: datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC") if timestamp else "—"


def clean_blocks(blocks: list[dict]) -> str:
    """Only explicitly typed visible blocks may enter storage; never save reasoning."""
    if not isinstance(blocks, list):
        raise TypeError("Content must be a list of typed blocks")
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            raise TypeError("Blocks must be objects")
        if block.get("type") in {"reasoning", "thinking"}:
            continue
        if block.get("type") not in {"text", "tool"} or not isinstance(block.get("text"), str):
            raise ValueError("Only text and tool blocks are supported")
        parts.append(block["text"])
    content = "\n".join(parts).strip()
    if len(content) > 100_000:
        raise ValueError("Turn exceeds 100,000 characters")
    return content


def create_app(config: Config, store: Store | None = None, models: LocalModels | None = None,
               start_worker: bool = True):
    store = store or Store(config.database)
    models = models or LocalModels(config.model_dir)
    worker = Worker(store, models)
    server = MCPServer("setauket", version="0.8.2", instructions=(
        "Shared, local memory. Register a persistent harness installation key and an agent first. "
        "Submit visible session turns explicitly; connecting alone does not capture transcripts. "
        "Search before assuming a previous decision is current."))

    @server.tool(description="Register one installation of a coding harness. Persist installation_key locally and reuse it on every reconnect. Then call register_agent; a connection does not create identity automatically.")
    async def register_harness(installation_key: str, name: str) -> dict:
        return {"harness_id": await anyio.to_thread.run_sync(store.register_harness, installation_key, name)}

    @server.tool(description="Register an agent or sub-agent within a registered harness; reuse external_id for the same agent. Pass parent_id for a sub-agent. Use the returned agent_id on every turn and memory write.")
    async def register_agent(harness_id: str, external_id: str, parent_id: str | None = None) -> dict:
        return {"agent_id": await anyio.to_thread.run_sync(store.register_agent, harness_id, external_id, parent_id)}

    @server.tool(description="Start a session of explicitly submitted turns. Set project_key to a stable repository identifier/path; store the session_id for subsequent append_turn and end_session calls. Connecting alone does not capture history.")
    async def start_session(harness_id: str, agent_id: str, project_key: str | None = None) -> dict:
        return {"session_id": await anyio.to_thread.run_sync(store.start_session, harness_id, agent_id, project_key)}

    @server.tool(description="Submit a visible conversation turn. Use a stable source_id so retries are safe; blocks are typed text/tool/reasoning/thinking objects with text. Reasoning/thinking blocks are discarded before storage. Call for each turn; MCP cannot capture turns automatically.")
    async def append_turn(session_id: str, harness_id: str, agent_id: str, source_id: str,
                          role: str, blocks: list[dict]) -> dict:
        content = clean_blocks(blocks)
        return {"turn_id": await anyio.to_thread.run_sync(store.add_turn, session_id, harness_id, agent_id, source_id, role, content)}

    @server.tool(description="Mark a session ended; new turns may reopen it until archival. An inactive session is summarized after 72 hours. Search_sessions retrieves it later.")
    async def end_session(session_id: str, harness_id: str) -> dict:
        await anyio.to_thread.run_sync(store.end_session, session_id, harness_id)
        return {"ended": True}

    @server.tool(description="Find past turns, archived summaries, and current decisions/preferences with lexical plus semantic search. Filter by project_key or category (hot/cold/memory). Use session_id from hot/cold results with get_session, or source_id from memory results with get_memory_history. Keyword search still works when models are unavailable.")
    async def search_sessions(query: str, project_key: str | None = None, category: str | None = None,
                              limit: int = 10) -> dict:
        if not query.strip() or len(query) > 2000:
            raise ValueError("Search query must contain 1–2000 characters")
        try:
            vector = await anyio.to_thread.run_sync(models.embed, query, True)
        except Exception as error:  # noqa: BLE001 - keyword recall must survive any model failure.
            log.warning("Semantic search unavailable: %s", type(error).__name__)
            vector = None
        results = await anyio.to_thread.run_sync(store.search_rows, query, project_key, category, limit, vector)
        return {"results": results, "semantic_available": vector is not None}

    @server.tool(description="Retrieve a session by session_id. Hot sessions have turns; archived sessions contain lossy generated summaries, one per 25-turn segment in order, not the original transcript. Session metadata and dates remain available.")
    async def get_session(session_id: str) -> dict:
        return {"session": await anyio.to_thread.run_sync(store.get_session, session_id)}

    @server.tool(description="Save an explicitly user-requested decision or preference. Do not save inferred agent conclusions. Set project_key for project-specific memories; omit for global. To change one, pass supersedes_id and inspect get_memory_history. This is local attribution, not authentication.")
    async def remember(kind: str, content: str, harness_id: str, agent_id: str,
                       user_requested: bool, project_key: str | None = None, supersedes_id: str | None = None) -> dict:
        if not user_requested:
            raise ValueError("Only explicit user-requested memories can be saved")
        memory_id = await anyio.to_thread.run_sync(store.remember, kind, content, harness_id, agent_id, project_key, supersedes_id)
        return {"memory_id": memory_id}

    @server.tool(description="Read a decision/preference revision chain from oldest to newest. Superseded records are retained for history but not returned by default search; use the last entry as current guidance.")
    async def get_memory_history(memory_id: str) -> dict:
        return {"history": await anyio.to_thread.run_sync(store.get_memory, memory_id)}

    def render(name, **context):
        return HTMLResponse(TEMPLATES.get_template(name).render(**context))

    @server.custom_route("/", methods=["GET"])
    async def dashboard(request: Request):
        with store.connect() as db:
            counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("sessions", "summaries", "memories", "turns", "chunks")}
            recent = [dict(row) for row in db.execute("SELECT s.id,s.started_at,s.archived_at,p.project_key FROM sessions s LEFT JOIN projects p ON s.project_id=p.id ORDER BY s.started_at DESC LIMIT 30")]
            failures = [dict(row) for row in db.execute("SELECT kind,source_id,attempts,error FROM jobs WHERE error IS NOT NULL ORDER BY id DESC LIMIT 10")]
        return render("dashboard.html", counts=counts, recent=recent, failures=failures)

    @server.custom_route("/memories", methods=["GET"])
    async def memories_page(request: Request):
        try:
            page = max(0, min(10000, int(request.query_params.get("page", "0"))))
        except ValueError:
            page = 0
        with store.connect() as db:
            memories = [dict(row) for row in db.execute(
                "SELECT m.*,p.project_key FROM memories m LEFT JOIN projects p ON m.project_id=p.id "
                "WHERE m.superseded_by IS NULL ORDER BY m.created_at DESC LIMIT 31 OFFSET ?", (page * 30,))]
        return render("memories.html", memories=memories[:30], page=page, has_next=len(memories) > 30)

    @server.custom_route("/sessions", methods=["GET"])
    async def sessions_page(request: Request):
        try:
            page = max(0, min(10000, int(request.query_params.get("page", "0"))))
        except ValueError:
            page = 0
        with store.connect() as db:
            sessions = [dict(row) for row in db.execute(
                "SELECT s.*,p.project_key FROM sessions s LEFT JOIN projects p ON p.id=s.project_id "
                "ORDER BY s.started_at DESC LIMIT 31 OFFSET ?", (page * 30,))]
        return render("sessions.html", sessions=sessions[:30], page=page, has_next=len(sessions) > 30)

    @server.custom_route("/search", methods=["GET"])
    async def search_page(request: Request):
        query = request.query_params.get("q", "")[:2000]
        project = request.query_params.get("project", "")[:500] or None
        category = request.query_params.get("category", "") or None
        results = []
        if query.strip():
            # Web search remains usable while models are offline.
            results = await anyio.to_thread.run_sync(store.search_rows, query, project, category, 30)
        return render("search.html", query=query, project=project or "", category=category or "", results=results)

    @server.custom_route("/sessions/{session_id}", methods=["GET"])
    async def session_page(request: Request):
        session = await anyio.to_thread.run_sync(store.get_session, request.path_params["session_id"])
        return render("session.html", session=session) if session else PlainTextResponse("Not found", status_code=404)

    @server.custom_route("/memories/{memory_id}", methods=["GET"])
    async def memory_page(request: Request):
        history = await anyio.to_thread.run_sync(store.get_memory, request.path_params["memory_id"])
        return render("memory.html", history=history) if history else PlainTextResponse("Not found", status_code=404)

    @server.custom_route("/config", methods=["GET"])
    async def config_page(request: Request):
        return render("config.html", config=config, embedding_ready=(models.embedding_path / "model_optimized.onnx").exists(),
                      text_ready=models.text_path.exists())

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"status": "ok"})

    @server.custom_route("/style.css", methods=["GET"])
    async def style(request: Request):
        return PlainTextResponse((Path(__file__).parent / "templates/style.css").read_text(), media_type="text/css")

    # SDK transport checks Host and Origin for /mcp. The outer middleware covers UI routes too.
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host=config.host,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        ),
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with sdk_lifespan(application):
            if start_worker:
                worker.start()
            try:
                yield
            finally:
                if start_worker:
                    await anyio.to_thread.run_sync(worker.stop)

    app.router.lifespan_context = lifespan
    app.state.store, app.state.models, app.state.worker, app.state.mcp = store, models, worker, server
    return app