FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DLSS5_HOST=0.0.0.0 \
    DLSS5_PORT=7860 \
    DLSS5_OPEN_BROWSER=0 \
    DLSS5_BACKEND=software

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg libglib2.0-0 ocl-icd-libopencl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-docker.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY app.py README.md README.ru.md ./
COPY dlss5_converter ./dlss5_converter
COPY tests ./tests

RUN useradd --create-home --uid 1000 converter \
    && mkdir -p /app/_work /app/jobs /app/outputs /app/originals \
    && chown -R converter:converter /app

USER converter
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/api/status', timeout=3)" || exit 1

CMD ["python", "app.py"]
