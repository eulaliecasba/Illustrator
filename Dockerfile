FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Koyeb provides $PORT; default to 8000 locally.
ENV PORT=8000
CMD gunicorn app:app --workers 2 --threads 4 --timeout 600 --bind 0.0.0.0:$PORT
