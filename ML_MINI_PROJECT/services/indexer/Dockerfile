FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/faiss_index

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "ingest.py"]
