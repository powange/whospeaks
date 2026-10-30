FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml ./
COPY whospeaks ./whospeaks
RUN pip install .

VOLUME /data
EXPOSE 10302 8095

HEALTHCHECK --interval=60s --timeout=5s --start-period=120s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/healthz', timeout=3)" || exit 1

CMD ["whospeaks"]
