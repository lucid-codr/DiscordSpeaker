FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates ffmpeg libopus0 pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --no-create-home bot

WORKDIR /app
COPY src/requirements.txt ./requirements.txt
RUN python -m pip install -r requirements.txt
COPY src/bot.py src/audio_ctl.sh ./
RUN chmod 644 bot.py && chmod 755 audio_ctl.sh

USER bot
CMD ["python", "bot.py"]
