FROM python:3.12-slim-bookworm

# ImageMagick for the dither step, emoji + fallback fonts so calendar icons render
RUN apt-get update \
 && apt-get install -y --no-install-recommends imagemagick fonts-noto-color-emoji fonts-noto-core fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright==1.63.0 \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY fonts-local.conf /etc/fonts/local.conf
RUN fc-cache -f

WORKDIR /app
COPY render.py /app/render.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s \
  CMD python3 -c "import json,urllib.request,sys; h=json.load(urllib.request.urlopen('http://localhost:8080/health',timeout=4)); sys.exit(0 if h['healthy'] else 1)"
CMD ["python3", "/app/render.py"]
