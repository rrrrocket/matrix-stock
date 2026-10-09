FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN STOCK_DEBUG=0 STOCK_SECRET_KEY=build-only-static-collection-key python manage.py collectstatic --noinput

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8020", "--workers", "2", "--threads", "4", "--access-logfile", "-", "--error-logfile", "-"]
