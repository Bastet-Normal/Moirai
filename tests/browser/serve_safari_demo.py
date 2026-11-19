"""Serve the offline demo with browser error collection installed before application scripts."""
import gzip
import html as html_tools
import re
import runpy
from pathlib import Path
from unittest.mock import patch

import uvicorn
from starlette.responses import Response

ROOT = Path(__file__).resolve().parents[2]
start_server = uvicorn.run


def serve(app, **options):
    @app.middleware("http")
    async def browser_diagnostics(request, call_next):
        if request.url.path == "/__layout_320":
            # Exercise the checked-in UI as a component at its 320px breakpoint.
            # Safari's desktop window chrome has a 336px minimum.
            source = (ROOT / "web/index.html").read_text(encoding="utf-8")
            source = source.replace("<head>", '<head><script src="/__safari_observer.js"></script>')
            frame = '<iframe title="320px layout fixture" style="border:0;width:320px;height:844px" srcdoc="' + html_tools.escape(source, quote=True) + '"></iframe>'
            return Response('<!doctype html><body style="margin:0">' + frame, media_type="text/html")
        if request.url.path == "/__safari_observer.js":
            return Response(Path(__file__).with_name("observer.js").read_bytes(), media_type="text/javascript")
        response = await call_next(request)
        prefix = request.query_params.get("safariFixture", "")
        if prefix:
            print("test download", request.url.path, response.status_code, response.headers.get("content-length"), flush=True)
        disposition = response.headers.get("content-disposition", "")
        if re.fullmatch(r"moirai-test-[0-9a-f-]{36}-", prefix) and 'filename="' in disposition:
            response.headers["content-disposition"] = disposition.replace('filename="', 'filename="' + prefix, 1)
        if request.url.path != "/" or not response.headers.get("content-type", "").startswith("text/html"):
            return response
        html = b"".join([chunk async for chunk in response.body_iterator])
        if response.headers.get("content-encoding") == "gzip":
            html = gzip.decompress(html)
        html = html.replace(b"<head>", b'<head><script src="/__safari_observer.js"></script>', 1)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        headers.pop("content-encoding", None)
        return Response(html, status_code=response.status_code, headers=headers)

    start_server(app, **options)


with patch.object(uvicorn, "run", serve):
    runpy.run_path(str(ROOT / "scripts/serve_demo.py"), run_name="__main__")
