#!/usr/bin/env bash
# Apply SQL migrations in order, tracking which have run.
# Uses `cat | docker exec -i` — redirecting a file into `docker exec` without
# -i silently no-ops with exit 0.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

CONTAINER=support_chatbot_db
PSQL="docker exec -i $CONTAINER psql -U ${POSTGRES_USER} -d ${POSTGRES_DB} -v ON_ERROR_STOP=1"

$PSQL -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
SQL

for f in infra/postgres/migrations/*.sql; do
    name=$(basename "$f")
    applied=$($PSQL -tAc "SELECT 1 FROM schema_migrations WHERE filename='$name'")
    if [ "$applied" = "1" ]; then
        echo "  skip  $name"
        continue
    fi
    echo "  apply $name"
    cat "$f" | $PSQL -q
    $PSQL -q -c "INSERT INTO schema_migrations (filename) VALUES ('$name')"
done
echo "migrations up to date"
