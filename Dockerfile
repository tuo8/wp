ARG PYTHON_BASE_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg aria2 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system mydrive \
    && useradd --system --gid mydrive --home-dir /home/mydrive --create-home mydrive

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY --chown=mydrive:mydrive mydrive /app/mydrive
RUN mkdir -p /data/storage /data/storage/tmp_uploads \
    && chown -R mydrive:mydrive /data/storage /home/mydrive

WORKDIR /app/mydrive
USER mydrive
EXPOSE 8765 8766 2022
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
