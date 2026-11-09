# teddybridge – Container-Image (Begleitcontainer neben TeddyCloud)
FROM python:3.13-slim
LABEL org.opencontainers.image.title="teddybridge" \
      org.opencontainers.image.description="Audiobookshelf, Navidrome und NAS nach TeddyCloud (TAF)" \
      org.opencontainers.image.source="https://github.com/urza-lab/teddybridge" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

# ffmpeg: m4b an Kapiteln schneiden, andere Formate als MP3 zusammenfügen (optional, aber im Image immer dabei)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin teddybridge
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LISTEN=0.0.0.0:8085
USER teddybridge
EXPOSE 8085
# /var/tmp: Arbeitsordner für Downloads (gross, darum nicht im RAM) – im Compose als Volume
VOLUME ["/var/tmp"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/login', timeout=8)" || exit 1
CMD ["python", "-m", "app"]
