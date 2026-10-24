"""Loopback-only acceptance fixture, with no Telegram client or credentials."""

import io
import json
import os

from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage
import uvicorn

from windows_app import security, service, settings

mcp = FastMCP("acceptance-fixture", stateless_http=True)


@mcp.tool()
async def ping(label: str) -> dict:
    return {"label": label, "pid": os.getpid()}


@mcp.tool()
async def image() -> Image:
    output = io.BytesIO()
    PILImage.new("RGB", (2, 2), "blue").save(output, format="PNG")
    return Image(data=output.getvalue(), format="png")


backend = service.LocalControl(
    mcp.streamable_http_app(),
    settings.token(),
    lambda: {"service": service.SERVICE_NAME, "pid": os.getpid(), "connected": True},
    lambda: None,
)


async def app(scope, receive, send):
    if scope["type"] == "http" and scope["path"] == "/mcp" and scope["method"] == "POST":
        from starlette.requests import Request
        from starlette.responses import PlainTextResponse

        body = await Request(scope, receive).body()
        request = json.loads(body)
        if request.get("params", {}).get("name") == "drop_transport":
            counter = settings.state() / "effects.txt"
            with counter.open("a", encoding="utf-8") as file:
                file.write("effect\n")
            return await PlainTextResponse("Connection failed after operation", status_code=503)(
                scope, receive, send
            )
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await backend(scope, replay, send)
    return await backend(scope, receive, send)


if __name__ == "__main__":
    security.prepare_endpoint()
    security.record_process()
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=settings.port(),
        access_log=False,
        log_level="error",
        ssl_certfile=str(security.certificate()),
        ssl_keyfile=str(security.private_key()),
    )
