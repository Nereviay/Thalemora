"""HTTP 只负责认证、参数和服务接线；无外部 principal/actor 身份入口。"""
import hmac
import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from .providers import ProviderUnavailable


def create_http(service, token):
    if not isinstance(token, str) or not token:
        raise ValueError("服务器管理 token 必须配置")
    reads = {name: getattr(service, name) for name in (
        "list_agents", "sources", "history", "search", "memory", "evidence", "water",
        "water_memberships", "inner_state", "capabilities", "local_search")}
    reads.update(provider_status=service.providers.status, usage=service.providers.audit)
    writes = {name: getattr(service, name) for name in (
        "register_agent", "grant_source", "import_history", "remember", "water_node", "water_move",
        "configure_persona", "reflect", "continuity", "build_local_index")}
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

    return Starlette(routes=[Route("/health", health),
                             Route("/api/{operation}", endpoint, methods=["GET", "POST"]),
                             Mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))])
