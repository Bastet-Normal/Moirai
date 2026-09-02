"""Bound request bytes before JSON/multipart parsing, without buffering a copy."""

import asyncio
import re
from collections.abc import AsyncIterator
from time import monotonic

from fastapi import HTTPException, Request
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_JSON_BODY_BYTES = 1024 * 1024
MAX_TEXT_BODY_BYTES = 16 * 1024 * 1024
MAX_BACKUP_UPLOAD_BYTES = 500 * 1024 * 1024
MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
BODY_CHUNK_TIMEOUT_SECONDS = 30
MAX_BODY_READ_SECONDS = 300


class BodyLimitExceeded(StarletteHTTPException, MultiPartException):
    """Also a parser error so Starlette closes partially spooled uploads."""

    def __init__(self, limit: int):
        self.message = f"请求体超过 {limit} 字节上限"
        StarletteHTTPException.__init__(self, status_code=413, detail=self.message)


class BodyReadTimeout(StarletteHTTPException, MultiPartException):
    def __init__(self):
        self.message = "请求体上传超时，请重新提交"
        StarletteHTTPException.__init__(self, status_code=408, detail=self.message)


class BodyReadInterrupted(StarletteHTTPException, MultiPartException):
    def __init__(self):
        self.message = "上传连接已中断"
        StarletteHTTPException.__init__(self, status_code=400, detail=self.message)


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"].rstrip("/")
        if path == "/api/novels/import":
            limit = MAX_BACKUP_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD_BYTES
        elif path == "/api/novel/patch_paragraph" or re.fullmatch(r"/api/novel/[^/]+/(chapter_text|para_rewrite_apply)", path):
            limit = MAX_TEXT_BODY_BYTES
        else:
            limit = MAX_JSON_BODY_BYTES
        lengths = [value for key, value in scope.get("headers", []) if key.lower() == b"content-length"]
        if lengths and (len(lengths) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 20):
            await JSONResponse({"detail": "Content-Length 请求头非法"}, status_code=400)(scope, receive, send)
            return
        if lengths and int(lengths[0]) > limit:
            await JSONResponse({"detail": f"请求体超过 {limit} 字节上限"}, status_code=413)(scope, receive, send)
            return

        received = 0
        body_complete = False
        deadline = monotonic() + MAX_BODY_READ_SECONDS

        async def bounded_receive():
            nonlocal received, body_complete
            if body_complete:
                return await receive()  # Not a timeout on subsequent model work/response streaming.
            budget = min(BODY_CHUNK_TIMEOUT_SECONDS, deadline - monotonic())
            try:
                if budget <= 0:
                    raise TimeoutError
                async with asyncio.timeout(budget):
                    message = await receive()
            except TimeoutError:
                timeout_error = BodyReadTimeout()
                raise timeout_error from None
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    error = BodyLimitExceeded(limit)
                    raise error
                body_complete = not message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                interrupted = BodyReadInterrupted()
                raise interrupted
            return message

        await self.app(scope, bounded_receive, send)


class BoundedBackupParser(MultiPartParser):
    """A single backup file; cap headers and file bytes while callbacks consume them."""

    def __init__(self, request: Request):
        super().__init__(request.headers, request.stream(), max_files=1, max_fields=0,
                         max_part_size=MAX_MULTIPART_OVERHEAD_BYTES)
        self.header_bytes = 0
        self.file_bytes = 0

    def _count_header(self, size: int) -> None:
        self.header_bytes += size
        if self.header_bytes > MAX_MULTIPART_OVERHEAD_BYTES:
            raise BodyLimitExceeded(MAX_MULTIPART_OVERHEAD_BYTES)

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        super().on_header_value(data, start, end)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self.file_bytes += end - start
        if self.file_bytes > MAX_BACKUP_UPLOAD_BYTES:
            raise BodyLimitExceeded(MAX_BACKUP_UPLOAD_BYTES)
        super().on_part_data(data, start, end)

    def close_files(self) -> None:
        # Starlette closes these on parser errors, but not every cancellation or
        # incomplete EOF. Keep this narrow adapter regression-tested on upgrades.
        for file in self._files_to_close_on_error:
            file.close()


async def backup_upload(request: Request) -> AsyncIterator[UploadFile]:
    """Parse exactly one file, with deterministic cleanup on parse/route failure."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "multipart/form-data":
        raise HTTPException(status_code=415, detail="备份导入需要 multipart/form-data 文件上传")
    if len(request.headers.get("content-type", "")) > 1000:
        raise HTTPException(status_code=400, detail="上传 Content-Type 请求头过长")
    parser = BoundedBackupParser(request)
    form = None
    try:
        try:
            form = await parser.parse()
        except (MultipartParseError, LookupError, UnicodeError) as exc:
            raise HTTPException(status_code=400, detail="multipart 上传格式或字符编码非法") from exc
        file = form.get("file")
        if len(form) != 1 or not isinstance(file, UploadFile):
            raise HTTPException(status_code=422, detail="请使用 file 字段上传一个备份文件")
        yield file
    except StarletteHTTPException:
        raise
    except MultiPartException as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    finally:
        try:
            if form is not None:
                await form.close()
        finally:
            parser.close_files()


BACKUP_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object", "required": ["file"],
            "properties": {"file": {"type": "string", "format": "binary"}},
            "additionalProperties": False,
        }}},
    },
}
