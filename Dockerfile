# Upstream tools are pinned to an immutable commit, with local thin transport code.
# The image can also run under Docker/Kubernetes without changing the app.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG UPSTREAM_COMMIT=6f7c33b1d23f8257814fd790341bc84cd88785e0
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/cookidoo-upstream
RUN git clone https://github.com/alexandrepa/mcp-cookidoo.git . \
    && git checkout --detach "${UPSTREAM_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${UPSTREAM_COMMIT}" \
    && rm -rf .git

COPY requirements-runtime.txt /app/requirements-runtime.txt
RUN uv sync --locked --no-dev \
    && uv pip install --python .venv/bin/python -r /app/requirements-runtime.txt

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 \
    /lambda-adapter /opt/extensions/lambda-adapter
COPY src /app/src

ENV PYTHONPATH=/app:/opt/cookidoo-upstream \
    PATH=/opt/cookidoo-upstream/.venv/bin:$PATH \
    PORT=8080 \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/health \
    AWS_LWA_INVOKE_MODE=response_stream \
    COOKIDOO_COUNTRY=gb \
    COOKIDOO_LANGUAGE=en-GB \
    COOKIDOO_DEVICE=TM7

WORKDIR /app
EXPOSE 8080
CMD ["uvicorn", "src.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
