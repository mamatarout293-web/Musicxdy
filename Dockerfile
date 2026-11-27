FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg decodes audio for voice chat; Node supports yt-dlp's YouTube challenge solver.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gcc libc6-dev ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Install the Rust POT provider and its matching yt-dlp plugin. The plugin invokes
# bgutil-pot on demand; no provider server is started.
RUN curl -fsSL https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-x86_64 \
        -o /usr/local/bin/bgutil-pot \
 && chmod +x /usr/local/bin/bgutil-pot \
 && /usr/local/bin/bgutil-pot --version \
 && curl -fsSL https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-ytdlp-pot-provider-rs.zip \
        -o /tmp/bgutil-ytdlp-pot-provider-rs.zip \
 && python -c "import site, zipfile; zipfile.ZipFile('/tmp/bgutil-ytdlp-pot-provider-rs.zip').extractall(site.getsitepackages()[0])" \
 && rm /tmp/bgutil-ytdlp-pot-provider-rs.zip

COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN node --version

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "main.py"]
