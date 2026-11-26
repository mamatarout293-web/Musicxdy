FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: decodes the audio for the voice chat | gcc: tgcrypto has no prebuilt wheel
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gcc libc6-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Node 22+: supported JavaScript runtime for yt-dlp's YouTube challenge solver
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN node --version

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "main.py"]
