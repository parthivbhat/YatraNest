"""Streaming body limit for the unauthenticated package enquiry form."""

from fastapi import HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.public_enquiries import MAX_FORM_BYTES


class PublicEnquiryBodyLimitMiddleware:
    """Reject oversized enquiry submissions before request.body buffers them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or not path.startswith("/packages/")
            or not path.endswith("/enquiries")
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > MAX_FORM_BYTES:
                    await self._respond_too_large(scope, send)
                    return
            except ValueError:
                await self._respond_invalid_length(scope, send)
                return

        received_bytes = 0

        async def limited_receive():
            nonlocal received_bytes
            message = await receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > MAX_FORM_BYTES:
                    raise HTTPException(status_code=413, detail="The submitted form is too large.")
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _respond_too_large(scope: Scope, send: Send) -> None:
        body = b'{"detail":"The submitted form is too large."}'
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _respond_invalid_length(scope: Scope, send: Send) -> None:
        body = b'{"detail":"The submitted form could not be read."}'
        await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
