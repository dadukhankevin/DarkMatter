FROM python:3.12-slim
COPY server.py /app/server.py
WORKDIR /app
RUN pip install --no-cache-dir "mcp[cli]" httpx uvicorn starlette cryptography anyio
ENV DARKMATTER_HOST=0.0.0.0
ENV DARKMATTER_TRANSPORT=http
ENV DARKMATTER_DISCOVERY=false
ENV DARKMATTER_ANCHOR_NODES=""
CMD ["python", "server.py"]
