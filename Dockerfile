FROM python:3.12-slim

ARG INSTALL_WAKE_EXTRAS=false
ARG INSTALL_COMMAND_VOSK=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VOICE_ASSISTANT_CONFIG=/app/data/config.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       alsa-utils \
       ca-certificates \
       curl \
       sudo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY assets ./assets

RUN pip install --no-cache-dir --upgrade pip \
    && if [ "$INSTALL_WAKE_EXTRAS" = "true" ] && [ "$INSTALL_COMMAND_VOSK" = "true" ]; then \
         pip install --no-cache-dir '.[wake-openwakeword,command-vosk]'; \
       elif [ "$INSTALL_WAKE_EXTRAS" = "true" ]; then \
         pip install --no-cache-dir '.[wake-openwakeword]'; \
       elif [ "$INSTALL_COMMAND_VOSK" = "true" ]; then \
         pip install --no-cache-dir '.[command-vosk]'; \
       else \
         pip install --no-cache-dir .; \
       fi

RUN mkdir -p /app/data /app/data/artifacts /app/assets/sounds \
    && useradd -m -u 10001 -G audio voiceassistant \
    && chown -R voiceassistant:voiceassistant /app

USER voiceassistant
EXPOSE 8080
VOLUME ["/app/data", "/app/assets/sounds"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/api/status >/dev/null || exit 1

CMD ["voice-assistant"]
