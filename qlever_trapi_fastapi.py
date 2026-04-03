#!/usr/bin/env python3
import argparse
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import json
from http import HTTPStatus
import threading
import time
from typing import Any
import urllib.error
import urllib.request
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from qlever_trapi import (
    answer_meta_knowledge_graph_request,
    answer_trapi_request,
    response_envelope,
)


DEFAULT_ASYNC_WORKERS = 4
DEFAULT_ASYNC_JOB_TTL_SECONDS = 3600
DEFAULT_METAKG_CACHE_SECONDS = 300


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the QLever TRAPI wrapper over FastAPI."
    )
    parser.add_argument(
        "--host-name",
        default="localhost",
        help="QLever host name. Default: localhost",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8888,
        help="QLever port. Default: 8888",
    )
    parser.add_argument(
        "--access-token",
        default=None,
        help="Optional QLever access token.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum number of matching edges to return. Default: 1000",
    )
    parser.add_argument(
        "--resource-id",
        default="infores:qlever-trapi",
        help="TRAPI `analysis.resource_id` value. Default: infores:qlever-trapi",
    )
    parser.add_argument(
        "--subclass-depth",
        type=int,
        default=1,
        help="Maximum endpoint subclass expansion depth. Use 0 to disable. Default: 1",
    )
    parser.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help="HTTP listen host. Default: 127.0.0.1",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=8000,
        help="HTTP listen port. Default: 8000",
    )
    parser.add_argument(
        "--async-workers",
        type=int,
        default=DEFAULT_ASYNC_WORKERS,
        help=f"Maximum concurrent async query workers. Default: {DEFAULT_ASYNC_WORKERS}",
    )
    parser.add_argument(
        "--async-job-ttl-seconds",
        type=int,
        default=DEFAULT_ASYNC_JOB_TTL_SECONDS,
        help=f"How long completed async jobs stay available. Default: {DEFAULT_ASYNC_JOB_TTL_SECONDS}",
    )
    parser.add_argument(
        "--metakg-cache-seconds",
        type=int,
        default=DEFAULT_METAKG_CACHE_SECONDS,
        help=f"Meta knowledge graph cache TTL in seconds. Default: {DEFAULT_METAKG_CACHE_SECONDS}",
    )
    return parser.parse_args()


def json_error_response(
    status: str,
    description: str,
    http_code: int,
) -> JSONResponse:
    return JSONResponse(
        status_code=http_code,
        content=response_envelope(
            status=status,
            description=description,
            http_code=http_code,
        ),
    )


async def read_json_request_body(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    body = json.loads(raw_body.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    return body


def runtime_config(
    qlever_host_name: str,
    qlever_port: int,
    access_token: str | None,
    limit: int,
    resource_id: str,
    subclass_depth: int,
    async_workers: int,
    async_job_ttl_seconds: int,
    metakg_cache_seconds: int,
) -> dict[str, Any]:
    return {
        "qlever_host_name": qlever_host_name,
        "qlever_port": qlever_port,
        "access_token": access_token,
        "limit": limit,
        "resource_id": resource_id,
        "subclass_depth": subclass_depth,
        "async_job_ttl_seconds": async_job_ttl_seconds,
        "metakg_cache_seconds": metakg_cache_seconds,
        "executor": ThreadPoolExecutor(
            max_workers=async_workers,
            thread_name_prefix="qlever-trapi-async",
        ),
        "jobs": {},
        "jobs_lock": threading.Lock(),
        "metakg_cache": {
            "expires_at": 0.0,
            "payload": None,
        },
        "metakg_cache_lock": threading.Lock(),
    }


def prune_jobs(runtime: dict[str, Any]) -> None:
    cutoff = time.time() - runtime["async_job_ttl_seconds"]
    with runtime["jobs_lock"]:
        expired_job_ids = [
            job_id
            for job_id, job in runtime["jobs"].items()
            if job["updated_at"] < cutoff and job["http_code"] != HTTPStatus.ACCEPTED
        ]
        for job_id in expired_job_ids:
            runtime["jobs"].pop(job_id, None)


def update_job(runtime: dict[str, Any], job_id: str, **updates: Any) -> dict[str, Any]:
    with runtime["jobs_lock"]:
        job = runtime["jobs"][job_id]
        job.update(updates)
        job["updated_at"] = time.time()
        return dict(job)


def get_job(runtime: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    prune_jobs(runtime)
    with runtime["jobs_lock"]:
        job = runtime["jobs"].get(job_id)
        return dict(job) if job is not None else None


def status_url(request: Request, job_id: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/asyncquery_status/{job_id}"


def async_status_payload(job_id: str, job: dict[str, Any], request: Request) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": job["status"],
        "description": job["description"],
        "http_code": job["http_code"],
        "job_id": job_id,
        "status_url": status_url(request, job_id),
    }
    if job.get("message") is not None:
        payload["message"] = job["message"]
    if job.get("callback"):
        payload["callback"] = job["callback"]
    if job.get("callback_error"):
        payload["callback_error"] = job["callback_error"]
    return payload


def callback_request_payload(message: dict[str, Any]) -> dict[str, Any]:
    return response_envelope(
        status="Success",
        description="Query processed successfully",
        http_code=HTTPStatus.OK,
        message=message["message"],
    )


def post_callback(callback_url: str, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        callback_url,
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 400:
            raise urllib.error.HTTPError(
                callback_url,
                response.status,
                f"Callback returned HTTP {response.status}",
                response.headers,
                None,
            )


def run_async_query(runtime: dict[str, Any], job_id: str, request_body: dict[str, Any]) -> None:
    query_request = dict(request_body)
    callback = query_request.pop("callback", None)
    update_job(
        runtime,
        job_id,
        status="Running",
        description="Query is still processing",
        http_code=HTTPStatus.ACCEPTED,
    )
    try:
        response = answer_trapi_request(
            query_request,
            host_name=runtime["qlever_host_name"],
            port=runtime["qlever_port"],
            access_token=runtime["access_token"],
            limit=runtime["limit"],
            resource_id=runtime["resource_id"],
            subclass_depth=runtime["subclass_depth"],
        )
        update_job(
            runtime,
            job_id,
            status="Success",
            description="Query processed successfully",
            http_code=HTTPStatus.OK,
            message=response["message"],
        )
        if isinstance(callback, str) and callback:
            try:
                post_callback(callback, callback_request_payload(response))
            except Exception as exc:
                update_job(runtime, job_id, callback_error=str(exc))
    except (ValueError, NotImplementedError) as exc:
        update_job(
            runtime,
            job_id,
            status="BadRequest",
            description=str(exc),
            http_code=HTTPStatus.BAD_REQUEST,
        )
    except urllib.error.URLError as exc:
        update_job(
            runtime,
            job_id,
            status="UpstreamError",
            description=f"QLever request failed: {exc}",
            http_code=HTTPStatus.BAD_GATEWAY,
        )
    except Exception as exc:
        update_job(
            runtime,
            job_id,
            status="InternalError",
            description=f"Unhandled server error: {exc}",
            http_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def submit_async_query(runtime: dict[str, Any], request_body: dict[str, Any]) -> str:
    prune_jobs(runtime)
    job_id = str(uuid.uuid4())
    submitted_at = time.time()
    with runtime["jobs_lock"]:
        runtime["jobs"][job_id] = {
            "status": "Accepted",
            "description": "Query accepted for asynchronous processing",
            "http_code": HTTPStatus.ACCEPTED,
            "message": None,
            "callback": request_body.get("callback"),
            "callback_error": None,
            "submitted_at": submitted_at,
            "updated_at": submitted_at,
        }
    runtime["executor"].submit(run_async_query, runtime, job_id, dict(request_body))
    return job_id


def get_meta_knowledge_graph(runtime: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    with runtime["metakg_cache_lock"]:
        cache = runtime["metakg_cache"]
        if cache["payload"] is not None and now < cache["expires_at"]:
            return cache["payload"]

    payload = answer_meta_knowledge_graph_request(
        host_name=runtime["qlever_host_name"],
        port=runtime["qlever_port"],
        access_token=runtime["access_token"],
    )
    with runtime["metakg_cache_lock"]:
        runtime["metakg_cache"] = {
            "expires_at": now + runtime["metakg_cache_seconds"],
            "payload": payload,
        }
    return payload


def create_fastapi_app(
    qlever_host_name: str = "localhost",
    qlever_port: int = 8888,
    access_token: str | None = None,
    limit: int = 1000,
    resource_id: str = "infores:qlever-trapi",
    subclass_depth: int = 1,
    async_workers: int = DEFAULT_ASYNC_WORKERS,
    async_job_ttl_seconds: int = DEFAULT_ASYNC_JOB_TTL_SECONDS,
    metakg_cache_seconds: int = DEFAULT_METAKG_CACHE_SECONDS,
) -> FastAPI:
    runtime = runtime_config(
        qlever_host_name=qlever_host_name,
        qlever_port=qlever_port,
        access_token=access_token,
        limit=limit,
        resource_id=resource_id,
        subclass_depth=subclass_depth,
        async_workers=async_workers,
        async_job_ttl_seconds=async_job_ttl_seconds,
        metakg_cache_seconds=metakg_cache_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            runtime["executor"].shutdown(wait=True)

    app = FastAPI(
        title="QLever TRAPI",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(
            status_code=HTTPStatus.OK,
            content=response_envelope(
                status="Success",
                description="TRAPI service is healthy",
                http_code=HTTPStatus.OK,
            ),
        )

    @app.post("/query")
    async def query_endpoint(request: Request) -> JSONResponse:
        try:
            request_body = await read_json_request_body(request)
            response = answer_trapi_request(
                request_body,
                host_name=runtime["qlever_host_name"],
                port=runtime["qlever_port"],
                access_token=runtime["access_token"],
                limit=runtime["limit"],
                resource_id=runtime["resource_id"],
                subclass_depth=runtime["subclass_depth"],
            )
        except json.JSONDecodeError as exc:
            return json_error_response("BadRequest", f"Invalid JSON: {exc}", HTTPStatus.BAD_REQUEST)
        except (ValueError, NotImplementedError) as exc:
            return json_error_response("BadRequest", str(exc), HTTPStatus.BAD_REQUEST)
        except urllib.error.URLError as exc:
            return json_error_response("UpstreamError", f"QLever request failed: {exc}", HTTPStatus.BAD_GATEWAY)
        except Exception as exc:
            return json_error_response(
                "InternalError",
                f"Unhandled server error: {exc}",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return JSONResponse(
            status_code=HTTPStatus.OK,
            content=response_envelope(
                status="Success",
                description="Query processed successfully",
                http_code=HTTPStatus.OK,
                message=response["message"],
            ),
        )

    @app.post("/asyncquery")
    async def asyncquery_endpoint(request: Request) -> JSONResponse:
        try:
            request_body = await read_json_request_body(request)
        except json.JSONDecodeError as exc:
            return json_error_response("BadRequest", f"Invalid JSON: {exc}", HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            return json_error_response("BadRequest", str(exc), HTTPStatus.BAD_REQUEST)

        job_id = submit_async_query(runtime, request_body)
        job = get_job(runtime, job_id)
        assert job is not None
        return JSONResponse(
            status_code=HTTPStatus.ACCEPTED,
            content=async_status_payload(job_id, job, request),
        )

    @app.get("/asyncquery_status/{job_id}")
    def asyncquery_status_endpoint(job_id: str, request: Request) -> JSONResponse:
        job = get_job(runtime, job_id)
        if job is None:
            return json_error_response(
                "NotFound",
                f"Unknown async query job: {job_id}",
                HTTPStatus.NOT_FOUND,
            )
        status_code = job["http_code"]
        return JSONResponse(
            status_code=status_code,
            content=async_status_payload(job_id, job, request),
        )

    @app.get("/meta_knowledge_graph")
    def meta_knowledge_graph_endpoint() -> JSONResponse:
        try:
            meta_knowledge_graph = get_meta_knowledge_graph(runtime)
        except urllib.error.URLError as exc:
            return json_error_response("UpstreamError", f"QLever request failed: {exc}", HTTPStatus.BAD_GATEWAY)
        except Exception as exc:
            return json_error_response(
                "InternalError",
                f"Unhandled server error: {exc}",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return JSONResponse(
            status_code=HTTPStatus.OK,
            content={
                "status": "Success",
                "description": "Meta knowledge graph generated successfully",
                "http_code": HTTPStatus.OK,
                "meta_knowledge_graph": meta_knowledge_graph,
            },
        )

    @app.get("/metakg")
    def metakg_endpoint() -> JSONResponse:
        return meta_knowledge_graph_endpoint()

    return app


def main() -> None:
    args = parse_args()
    uvicorn.run(
        create_fastapi_app(
            qlever_host_name=args.host_name,
            qlever_port=args.port,
            access_token=args.access_token,
            limit=args.limit,
            resource_id=args.resource_id,
            subclass_depth=args.subclass_depth,
            async_workers=args.async_workers,
            async_job_ttl_seconds=args.async_job_ttl_seconds,
            metakg_cache_seconds=args.metakg_cache_seconds,
        ),
        host=args.listen_host,
        port=args.listen_port,
    )


if __name__ == "__main__":
    main()
