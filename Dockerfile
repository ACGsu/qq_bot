FROM docker.1ms.run/library/python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache ffmpeg

COPY bot.py .
COPY plugin_control.py .
COPY plugin_config.json .
COPY plugins ./plugins
COPY assets ./assets

CMD ["python", "bot.py"]
