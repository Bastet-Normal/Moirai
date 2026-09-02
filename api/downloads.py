"""Per-request download files with a lifetime bounded by the HTTP response."""

import tempfile
from collections.abc import Callable
from pathlib import Path

from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send


class TemporaryDownloadResponse(FileResponse):
    def __init__(self, directory: tempfile.TemporaryDirectory, path: Path, *, media_type: str):
        super().__init__(path, filename=path.name, media_type=media_type)
        self._directory = directory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Stream the file ourselves so a server's deferred path-send cannot
        # outlive the temporary file. Cleanup also runs on range errors and
        # disconnect/cancellation, where a normal background task may not run.
        scope = {**scope, "extensions": {
            key: value for key, value in scope.get("extensions", {}).items()
            if key != "http.response.pathsend"
        }}
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._directory.cleanup()


def build_download(render: Callable[[Path], Path], *, media_type: str) -> TemporaryDownloadResponse:
    directory = tempfile.TemporaryDirectory(prefix="novelengine-download-")
    try:
        root = Path(directory.name).resolve()
        path = render(root).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Download renderer must return a file inside its temporary directory")
        return TemporaryDownloadResponse(directory, path, media_type=media_type)
    except BaseException:
        directory.cleanup()
        raise
