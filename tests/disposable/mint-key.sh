#!/bin/sh
# Print a fresh API key for the disposable instance's admin user.
set -e
cd "$(dirname "$0")"
docker compose -p almcp-test -f compose.yaml exec -T server python manage.py shell -c \
  "from users.models import APIKey, CustomUser; print(APIKey.generate(CustomUser.objects.get(username='admin'), 'it')[1])" \
  2>/dev/null | grep '^al_' | tail -1
