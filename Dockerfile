FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/fde PIP_DEFAULT_TIMEOUT=180
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml /app/
COPY src /app/src
COPY migrations /app/migrations
COPY alembic.ini /app/
RUN pip install --no-cache-dir --no-deps . \
    && groupadd --gid 10001 fde && useradd --uid 10001 --gid fde --create-home fde \
    && mkdir -p /app/.data && chown -R fde:fde /app/.data /home/fde
USER 10001:10001
EXPOSE 8800
CMD ["fde", "serve", "--host", "0.0.0.0", "--port", "8800"]
