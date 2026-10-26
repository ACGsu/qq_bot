ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/bot.lock ./requirements/bot.lock
RUN python -m pip install --no-cache-dir --disable-pip-version-check --timeout 120 --retries 5 \
    --require-hashes -r requirements/bot.lock

COPY bot.py pyproject.toml ./
# Runtime config is bind-mounted by Compose; never bake private settings into the image.
COPY plugins ./plugins
COPY services ./services
COPY infrastructure ./infrastructure
COPY assets ./assets
RUN mkdir -p config storage/db storage/downloads storage/cache storage/backups storage/webui

EXPOSE 8080
CMD ["python", "bot.py"]
