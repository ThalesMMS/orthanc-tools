from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request


class OrthancApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str):
        self.method = method
        self.path = path
        self.status = status
        self.body = body.strip()
        super().__init__(f"{method} {path} failed with HTTP {status}: {self.body}")


class OrthancNetworkError(RuntimeError):
    def __init__(self, method: str, path: str, reason: Any):
        self.method = method
        self.path = path
        self.reason = reason
        super().__init__(f"{method} {path} failed: {reason}")


class OrthancRestClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 60.0):
        self.base_url = base_url
        self.timeout = timeout
        credentials = f"{username}:{password}".encode("utf-8")
        self._auth_header = f"Basic {base64.b64encode(credentials).decode('ascii')}"

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _decode_body(self, response: Any, payload: bytes) -> Any:
        if not payload:
            return None
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type or payload[:1] in (b"{", b"["):
            return json.loads(payload.decode("utf-8"))
        return payload.decode("utf-8", errors="replace")

    def request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        content_type: str = "application/json",
        accept: str = "application/json",
        stream_to: Path | None = None,
        stream_handle: Any = None,
    ) -> Any:
        data = None
        headers = {
            "Authorization": self._auth_header,
            "Accept": accept,
        }
        if payload is not None:
            if isinstance(payload, (bytes, bytearray)):
                data = bytes(payload)
                headers["Content-Type"] = content_type
            else:
                data = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = content_type
        req = request.Request(self._url(path), data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                if stream_to is not None or stream_handle is not None:
                    bytes_written = 0
                    handle = stream_handle
                    close_when_done = False
                    if handle is None:
                        handle = stream_to.open("wb")
                        close_when_done = True
                    try:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            handle.write(chunk)
                            bytes_written += len(chunk)
                        if hasattr(handle, "flush"):
                            handle.flush()
                        if hasattr(handle, "fileno"):
                            os.fsync(handle.fileno())
                    finally:
                        if close_when_done:
                            handle.close()
                    return {
                        "status": getattr(response, "status", 200),
                        "content_type": response.headers.get("Content-Type", ""),
                        "bytes_written": bytes_written,
                    }
                return self._decode_body(response, response.read())
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise OrthancApiError(method, path, exc.code, body) from exc
        except error.URLError as exc:
            raise OrthancNetworkError(method, path, exc.reason) from exc

    def get(self, path: str, accept: str = "application/json") -> Any:
        return self.request("GET", path, accept=accept)

    def post(
        self,
        path: str,
        payload: Any = None,
        content_type: str = "application/json",
        accept: str = "application/json",
    ) -> Any:
        return self.request("POST", path, payload=payload, content_type=content_type, accept=accept)

    def put(self, path: str, payload: Any = None) -> Any:
        return self.request("PUT", path, payload=payload)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)
