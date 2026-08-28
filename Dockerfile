FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md LICENSE /app/
COPY darkmatter /app/darkmatter
RUN pip install --no-cache-dir --no-deps .

RUN useradd --system --no-create-home darkmatter \
    && mkdir -p /data/.darkmatter \
    && chown -R darkmatter:darkmatter /data
USER darkmatter

ENV HOME=/data
ENV DARKMATTER_PROJECT_DIR=/data

CMD ["python", "-m", "darkmatter"]
