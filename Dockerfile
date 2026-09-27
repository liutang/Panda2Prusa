FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    P2P_HOST=0.0.0.0 \
    P2P_PORT=8543

WORKDIR /app

COPY requirements.txt requirements-web.txt ./
RUN pip install -r requirements-web.txt

COPY panda2prusa ./panda2prusa

RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8543

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"P2P_PORT\"]}/healthz', timeout=4)" || exit 1

CMD ["python", "-m", "panda2prusa.web"]
