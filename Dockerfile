FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py /app/server.py

# Run as non-root with a writable data directory for state files
RUN useradd --system --no-create-home darkmatter \
    && mkdir -p /data \
    && chown darkmatter:darkmatter /data
USER darkmatter

ENV DARKMATTER_HOST=0.0.0.0
ENV DARKMATTER_TRANSPORT=http
ENV DARKMATTER_DISCOVERY=false
ENV DARKMATTER_ANCHOR_NODES=""
ENV HOME=/data

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/.well-known/darkmatter.json')" || exit 1

CMD ["python", "server.py"]
