FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && \
    useradd --system --uid 10001 --create-home bidreader && \
    mkdir -p /data && chown -R bidreader:bidreader /app /data

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "bidreader.app:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
