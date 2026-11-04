"""AWS Lambda Function URL entry point for the read-only FastMCP server.

Uses buffered JSON HTTP responses; does not need Docker, an HTTP sidecar,
a Lambda Web Adapter, or a running Uvicorn server.
"""
from mangum import Mangum

from src.app import create_app

handler = Mangum(create_app(), lifespan="on")
