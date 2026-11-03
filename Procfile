web: gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 3 --timeout 90
worker: python manage.py auto_release_escrow
