FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV HOST=0.0.0.0 \
    PORT=9091 \
    STATE_DIR=/data/ts-bridge

EXPOSE 9091

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9091"]


