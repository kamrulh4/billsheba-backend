#!/usr/bin/env bash

python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py seed_plans --if-empty
gunicorn --bind 0.0.0.0:8000 --workers 3 --threads 2 app.wsgi:application