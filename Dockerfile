# ベースイメージとして公式のPythonイメージを使用
FROM python:3.9-slim

ENV PORT 8080
ENV PYTHONUNBUFFERED TRUE

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 main:app
