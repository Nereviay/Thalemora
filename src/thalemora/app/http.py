"""HTTP 只负责认证、参数和服务接线；无外部 principal/actor 身份入口。"""
import hmac
import json
import os
from functools import partial
import anyio
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from .providers import ProviderUnavailable
from .mcp import create_mcp
from .recall_tokens import InvalidRecallToken

RECALL_TIMEOUT_SECONDS = 5


def create_http(service, token):
    if not isinstance(token, str) or not token:
        raise ValueError("服务器管理 token 必须配置")
    mcp = create_mcp(service)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    class ProtectedMCP:
        async def __call__(self, scope, receive, send):
            authorization = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(authorization, ("Bearer " + token).encode()):
                response = JSONResponse({"error": "管理身份认证失败"}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
            await mcp_app(scope, receive, send)

    protected_mcp = ProtectedMCP()

    reads = {name: getattr(service, name) for name in (
        "list_agents", "sources", "history", "search", "memory", "evidence", "water",
        "water_memberships", "inner_state", "capabilities", "local_search")}
    reads.update(provider_status=service.providers.status, usage=service.providers.audit)
    writes = {name: getattr(service, name) for name in (
        "register_agent", "grant_source", "import_history", "remember", "water_node", "water_move",
        "configure_persona", "reflect", "continuity", "build_local_index",
        "issue_recall_token", "revoke_recall_token")}
    writes.update(configure_provider=service.providers.configure, call_provider=service.providers.call)

    async def endpoint(request):
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
            return JSONResponse({"error": "管理身份认证失败"}, status_code=401)
        operation = request.path_params["operation"]
        handler = (reads if request.method == "GET" else writes).get(operation)
        if handler is None:
            return JSONResponse({"error": "未知操作"}, status_code=404)
        try:
            if request.method == "GET":
                args = dict(request.query_params)
                for key in ("limit", "offset", "card_id"):
                    if key in args:
                        args[key] = int(args[key])
                if "include_deleted" in args:
                    if args["include_deleted"] not in ("true", "false"):
                        raise ValueError("include_deleted 须为 true/false")
                    args["include_deleted"] = args["include_deleted"] == "true"
            else:
                chunks = bytearray()
                async for chunk in request.stream():
                    chunks.extend(chunk)
                    if len(chunks) > 1024 * 1024:
                        return JSONResponse({"error": "每批请求最多 1 MiB"}, status_code=413)
                args = json.loads(chunks)
                if not isinstance(args, dict):
                    raise ValueError("请求须为 JSON 对象")
            result = await run_in_threadpool(handler, **args)
            return JSONResponse({"result": result})
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except ProviderUnavailable as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def health(request):
        return JSONResponse({"status": "ok"})

    async def recall(request):
        if request.method != "POST":
            return JSONResponse({"error": "method_not_allowed"}, status_code=405,
                                headers={"Allow": "POST"})

        def failure(status, code):
            return JSONResponse({"error": code}, status_code=status)

        async def execute():
            authorization = request.headers.get("authorization", "")
            if not authorization.startswith("Bearer "):
                raise InvalidRecallToken()
            scope = await anyio.to_thread.run_sync(
                partial(service.recall_tokens.resolve, authorization[7:]), abandon_on_cancel=True)
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > 1024 * 1024:
                    return failure(413, "request_too_large")
            args = json.loads(chunks)
            allowed = {"message", "visible_memory_ids", "visible_archive_ids", "visible_texts"}
            if not isinstance(args, dict) or set(args) - allowed or "message" not in args:
                return failure(400, "invalid_request")
            if not isinstance(args["message"], str) or len(args["message"]) > 200:
                return failure(400, "invalid_message")
            for key, item_type in (("visible_memory_ids", int), ("visible_archive_ids", str),
                                   ("visible_texts", str)):
                if key in args and (not isinstance(args[key], list) or
                                    any(type(v) is not item_type for v in args[key])):
                    return failure(400, "invalid_visible_items")
            message = args.pop("message")
            result = await anyio.to_thread.run_sync(
                partial(service.recall_for_turn, **scope, query=message, **args), abandon_on_cancel=True)
            if result["errors"] and not result.get("rendered"):
                return failure(503, "recall_unavailable")
            # 可选 Rerank 出错仍可使用基础召回，不能让它拖垮整轮消息。
            warnings = ["rerank_unavailable"] if result["errors"] else []
            # 仅输出插入文本及引用标识，不透传内部 score、正文或异常。
            refs = {kind: [{k: item[k] for k in fields if k in item} for item in result[kind]]
                    for kind, fields in (("memories", ("id", "revision_id")),
                                         ("archive", ("id", "version")))}
            return JSONResponse(dict(rendered=result["rendered"], **refs,
                                     empty=not bool(result["rendered"]), errors=warnings))

        try:
            with anyio.fail_after(RECALL_TIMEOUT_SECONDS):
                return await execute()
        except InvalidRecallToken:
            return failure(401, "invalid_recall_token")
        except PermissionError:
            return failure(403, "source_not_authorized")
        except (ValueError, TypeError):
            return failure(400, "invalid_request")
        except TimeoutError:
            return failure(504, "recall_timeout")
        except ProviderUnavailable:
            return failure(503, "recall_unavailable")
        except Exception:
            return failure(500, "recall_failed")

    app = Starlette(routes=[Route("/health", health),
                             Route("/api/recall", recall, methods=["GET", "POST"]),
                             Route("/api/{operation}", endpoint, methods=["GET", "POST"]),
                             Route("/mcp", protected_mcp),
                             Route("/mcp/", protected_mcp),
                             Mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))],
                     lifespan=lifespan)
    # 仅在管理员显式指定可信网页 Origin 时，允许浏览器连接本地召回。
    if origin := os.environ.get("THALEMORA_RECALL_CORS_ORIGIN"):
        app.add_middleware(CORSMiddleware, allow_origins=[origin],
                           allow_methods=["POST"], allow_headers=["Authorization", "Content-Type"])
    return app
