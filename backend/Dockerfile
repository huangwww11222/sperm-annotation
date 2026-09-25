FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# OpenCV/AVI runtime dependencies.  The SAM3 CUDA libraries are supplied by
# the PyTorch wheel and the NVIDIA container runtime exposes the host driver.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

COPY backend/requirements.txt ./requirements.txt

ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu132
RUN python -m pip install --upgrade pip \
    && python -m pip install \
       torch==2.14.0 torchvision==0.29.0 \
       --index-url "${PYTORCH_INDEX_URL}" \
    && python -m pip install -r requirements.txt

COPY backend/app ./app
COPY backend/__init__.py ./__init__.py

RUN mkdir -p /data/storage /data/database

ENV BACKEND_HOST=0.0.0.0 \
    BACKEND_PORT=3000 \
    APP_STORAGE_DIR=/data/storage \
    APP_DB_FILE=/data/database/app.db \
    SAM3_MODEL_ID=/models/sam3

EXPOSE 3000

# Keep exactly one worker: GPU scheduling, task status and tracking locks are
# currently process-local.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000", "--workers", "1"]
