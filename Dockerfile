# teddybridge – container image (companion container next to TeddyCloud)
FROM python:3.13-slim
LABEL org.opencontainers.image.title="teddybridge" \
      org.opencontainers.image.description="Audiobookshelf, Navidrome and NAS to TeddyCloud (TAF)" \
      org.opencontainers.image.source="https://github.com/urza-lab/teddybridge" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version="1.3.0"

# ffmpeg: cut m4b at chapters, merge formats other than MP3 (optional, but always included in the image)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin teddybridge
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/

# /config: settings from the setup in the browser (since 1.2); mount a volume there
RUN mkdir /config && chown teddybridge:teddybridge /config && chmod 700 /config
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LISTEN=0.0.0.0:8085 TB_CONFIG_FILE=/config/teddybridge.env
USER teddybridge
EXPOSE 8085
# /var/tmp: work folder for downloads (large, hence not in RAM) – a volume in compose
VOLUME ["/var/tmp"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/login', timeout=8)" || exit 1
CMD ["python", "-m", "app"]
