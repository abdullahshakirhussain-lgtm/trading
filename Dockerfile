FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    LOG_DIR=/tmp/logs

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY copilot/ ./copilot/
COPY run.py .

# /data is a Railway volume mount — SQLite must live here or state is lost on redeploy
RUN mkdir -p /data /tmp/logs

CMD ["python", "run.py"]
