"""Bound request bodies on staff blog post create/edit endpoints."""

from starlette.types import ASGIApp, Receive, Scope, Send


MAX_BLOG_REQUEST_BYTES = 6 * 1024 * 1024
_BLOG_CREATE = "/admin/blog"
_BLOG_EDIT_SUFFIX = "/edit"


class BlogUploadBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or scope.get("method") != "POST" or not (
            path == _BLOG_CREATE or (path.startswith("/admin/blog/") and path.endswith(_BLOG_EDIT_SUFFIX))
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > MAX_BLOG_REQUEST_BYTES:
                    await self._too_large(send)
                    return
            except ValueError:
                await self._bad_request(send)
                return

        total = 0

        async def receive_limited():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > MAX_BLOG_REQUEST_BYTES:
                    # Cancel the stream; Starlette's multipart parser will not
                    # receive an unbounded body from chunked requests.
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, receive_limited, send)
        except _BodyTooLarge:
            await self._too_large(send)

    @staticmethod
    async def _too_large(send: Send) -> None:
        body = b'{"detail":"Blog submission exceeds the allowed size."}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _bad_request(send: Send) -> None:
        body = b'{"detail":"The submitted form could not be read."}'
        await send({"type": "http.response.start", "status": 400,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    pass
