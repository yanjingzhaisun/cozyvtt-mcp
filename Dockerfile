FROM python:3.12-slim

WORKDIR /app
COPY uv.lock ./uv.lock
COPY scripts/docker_requirements.py ./scripts/docker_requirements.py
# Wheels come from uv.lock as direct URLs with sha256 hashes; only the host serving those blobs
# is configurable. Default is the canonical CDN; mainland-China builders can use a mirror:
#   docker build --build-arg WHEEL_BASE=https://mirrors.aliyun.com/pypi/packages .
# Mirrors serve byte-identical blobs, so the lock's hashes still verify.
ARG WHEEL_BASE=https://files.pythonhosted.org/packages
# Install only compatible runtime wheels already recorded in the lock, with hashes.
# --no-deps/--no-index prevent dependency resolution or unlocked build downloads.
RUN python scripts/docker_requirements.py --wheel-base "$WHEEL_BASE" > /tmp/requirements.txt \
    && python -m pip install --disable-pip-version-check --no-cache-dir \
       --no-index --no-deps --only-binary=:all: --require-hashes \
       -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY server.py auth.py client.py ws_listener.py ./
COPY tools/ ./tools/
ENTRYPOINT ["python", "server.py"]
