FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.32" \
      "psycopg[binary,pool]>=3.2" "httpx>=0.28" \
      "pydantic>=2.13" "selectolax>=0.4"

COPY src/ /app/src/
ENV PYTHONPATH=/app/src

EXPOSE 8080
CMD ["uvicorn", "fadiaapi.main:app", "--host", "0.0.0.0", "--port", "8080"]
