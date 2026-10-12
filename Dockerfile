# Problem Radar v kontajneri: agent, ktorý číta web, nemá prístup k tvojim súborom, e-mailu ani heslám.
# Vidí len config.yaml (iba na čítanie) a priečinky data/, reports/, logs/. Model beží na hostiteľovi (Ollama).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY radar ./radar

RUN useradd --create-home --uid 1000 radar && mkdir -p /app/work && chown radar /app/work
USER radar
ENTRYPOINT ["python", "-m", "radar", "--config", "/app/work/config.yaml"]
CMD ["run-all"]
