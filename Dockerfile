# Build the BgUtils PO Token provider script used by yt-dlp on headless servers.
FROM node:22-bookworm-slim AS pot-provider
RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 make g++ \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider \
 && cd /opt/bgutil-ytdlp-pot-provider/server \
 && npm ci \
 && npx tsc

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg decodes audio for voice chat; native libraries support the PO token provider.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gcc libc6-dev ca-certificates libstdc++6 libcairo2 libpango-1.0-0 libjpeg62-turbo libgif7 librsvg2-2 \
 && rm -rf /var/lib/apt/lists/*

# Node 22 is used by yt-dlp EJS and the BgUtils token provider.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=pot-provider /opt/bgutil-ytdlp-pot-provider/server /root/bgutil-ytdlp-pot-provider/server
RUN node --version

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "main.py"]
