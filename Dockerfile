FROM docker.1ms.run/library/python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Noto CJK provides redistributable Chinese glyphs; no browser or Windows fonts.
# Override only at build time when the official Alpine CDN is unreachable.
ARG ALPINE_MIRROR=https://dl-cdn.alpinelinux.org/alpine
# BuildKit keeps this download cache out of the final image. Predownload before
# committing packages, with bounded retries for interrupted network transfers.
RUN --mount=type=cache,target=/var/cache/apk,sharing=locked \
    case "$ALPINE_MIRROR" in https://*) ;; *) echo "ALPINE_MIRROR must use HTTPS" >&2; exit 1 ;; esac \
    && alpine_series="$(cut -d. -f1,2 /etc/alpine-release)" \
    && printf '%s/v%s/main\n%s/v%s/community\n' \
        "${ALPINE_MIRROR%/}" "$alpine_series" "${ALPINE_MIRROR%/}" "$alpine_series" > /etc/apk/repositories \
    && installed=0 \
    && for attempt in 1 2 3; do \
        if apk --timeout 30 --cache-dir /var/cache/apk --cache-predownload --cache-packages \
            add --update-cache ffmpeg font-noto-cjk; then \
            installed=1; break; \
        fi; \
        echo "Alpine package download/install failed (attempt $attempt/3)" >&2; \
        if [ "$attempt" -lt 3 ]; then sleep 2; fi; \
    done \
    && [ "$installed" -eq 1 ]

COPY requirements/bot.txt ./requirements/bot.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements/bot.txt

COPY bot.py .
COPY plugin_control.py .
# Runtime config is bind-mounted by Compose; never bake private settings into the image.
COPY plugins ./plugins
COPY assets ./assets

CMD ["python", "bot.py"]
