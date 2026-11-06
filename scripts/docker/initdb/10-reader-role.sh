#!/bin/sh
# Runs once, when the Postgres volume is first created. Creates the read-only role the
# question box uses. Grants are applied by migration 005 after the tables exist.
set -eu
psql -v ON_ERROR_STOP=1 -v pw="$POSTGRES_READER_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE devtrack_reader LOGIN PASSWORD :'pw';
ALTER ROLE devtrack_reader SET default_transaction_read_only = on;
ALTER ROLE devtrack_reader SET statement_timeout = '5s';
SQL
