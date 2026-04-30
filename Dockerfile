FROM python:3.12-slim

WORKDIR /app
RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/

ARG SERVICE
COPY ${SERVICE}/ ./${SERVICE}/

USER appuser
ENV PORT=8080

# Shell form so ${SERVICE} is expanded at runtime
CMD uvicorn ${SERVICE}.main:app --host 0.0.0.0 --port ${PORT}
