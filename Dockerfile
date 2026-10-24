FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sleepbot sleepbot
ENV DB_PATH=/data/sleep.db PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "-m", "sleepbot"]
