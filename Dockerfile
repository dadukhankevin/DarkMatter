FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install the darkmatter package itself
COPY pyproject.toml README.md LICENSE /app/
COPY darkmatter /app/darkmatter
RUN pip install --no-cache-dir --no-deps .

# Run as non-root with a writable data dir for passport + state files.
# /data holds .darkmatter/passport.key and the state file — mount a volume
# here to keep the bootstrap's identity stable across redeploys.
RUN useradd --system --no-create-home darkmatter \
    && mkdir -p /data/.darkmatter \
    && chown -R darkmatter:darkmatter /data
USER darkmatter

ENV HOME=/data
ENV DARKMATTER_PROJECT_DIR=/data
ENV DARKMATTER_HOST=0.0.0.0
ENV DARKMATTER_TRANSPORT=http
ENV DARKMATTER_DISCOVERY=false
# Bootstrap rendezvous: auto-accept signed connection requests from any peer.
ENV DARKMATTER_BOOTSTRAP_MODE=true
# A rendezvous node serves many agents — the default cap of 50 fills up fast
# and starts refusing connections. Raise it well above that.
ENV DARKMATTER_MAX_CONNECTIONS=2000
# Don't chase the default public bootstrap from inside the bootstrap itself.
ENV DARKMATTER_BOOTSTRAP_PEERS=""
# NOTE: do NOT set DARKMATTER_TRUST_PROXY here. Behind a load balancer the
# socket IP is the proxy, so the local admin API stays 403-gated from the
# public internet — exactly what we want for a public node.

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/.well-known/darkmatter.json')" || exit 1

CMD ["python", "-m", "darkmatter"]
