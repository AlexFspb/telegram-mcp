"""Loopback-only acceptance fixture, with no Telegram client or credentials."""

import io
import os

from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage
import uvicorn

from windows_app import service, settings

mcp = FastMCP("acceptance-fixture", stateless_http=True)


@mcp.tool()
async def ping(label: str) -> dict:
    return {"label": label, "pid": os.getpid()}


@mcp.tool()
async def image() -> Image:
    output = io.BytesIO()
    PILImage.new("RGB", (2, 2), "blue").save(output, format="PNG")
    return Image(data=output.getvalue(), format="png")


app = service.LocalControl(
    mcp.streamable_http_app(),
    settings.token(),
    lambda: {"service": service.SERVICE_NAME, "pid": os.getpid(), "connected": True},
    lambda: None,
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=settings.port(), access_log=False, log_level="error")
