FROM python:3.12-slim

WORKDIR /app
COPY uv.lock ./uv.lock
COPY scripts/docker_requirements.py ./scripts/docker_requirements.py
# Install only compatible runtime wheels already recorded in the lock, with hashes.
# --no-deps/--no-index prevent dependency resolution or unlocked build downloads.
RUN python scripts/docker_requirements.py > /tmp/requirements.txt \
    && python -m pip install --disable-pip-version-check --no-cache-dir \
       --no-index --no-deps --only-binary=:all: --require-hashes \
       -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY server.py auth.py client.py ws_listener.py ./
COPY tools/ ./tools/
ENTRYPOINT ["python", "server.py"]
