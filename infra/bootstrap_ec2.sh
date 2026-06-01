#!/bin/bash
# EC2 user-data: Postgres + pgvector + pgbouncer for LandIQ RAG.
# NOTE: untested on a live instance yet; validate package names / versions on the
# first deploy (AL2023 ships postgresql15; pgvector is built from source here).
# /etc/landiq/rag.env (written before this script) provides BACKUP_BUCKET, SSM_PREFIX.
set -euxo pipefail
source /etc/landiq/rag.env

PGDATA=/var/lib/pgsql/data
DB_PASSWORD="$(aws ssm get-parameter --name "${SSM_PREFIX}/db_password" --with-decryption \
  --query Parameter.Value --output text 2>/dev/null || echo CHANGEME)"

# --- Packages ---------------------------------------------------------------
dnf install -y postgresql15-server postgresql15 postgresql15-contrib \
  postgresql15-devel gcc make git redhat-rpm-config pgbouncer awscli

# --- pgvector from source ---------------------------------------------------
cd /tmp
git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git
cd pgvector && make && make install
cd / && rm -rf /tmp/pgvector

# --- Initialise Postgres ----------------------------------------------------
/usr/bin/postgresql-setup --initdb
systemctl enable --now postgresql

sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
  DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'landiq') THEN
      CREATE ROLE landiq LOGIN PASSWORD '${DB_PASSWORD}';
    END IF;
  END \$\$;
  SELECT 'CREATE DATABASE landiq_rag OWNER landiq'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'landiq_rag')\gexec
SQL
sudo -u postgres psql -d landiq_rag -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto;"

# Allow connections from the VPC (pgbouncer connects on localhost; Lambdas hit pgbouncer).
echo "listen_addresses = 'localhost'" >> "${PGDATA}/postgresql.conf"
echo "host all all 127.0.0.1/32 scram-sha-256" >> "${PGDATA}/pg_hba.conf"
systemctl restart postgresql

# --- pgbouncer (transaction pooling; bounds Lambda connection storms) -------
cat > /etc/pgbouncer/pgbouncer.ini <<INI
[databases]
landiq_rag = host=127.0.0.1 port=5432 dbname=landiq_rag
[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
max_client_conn = 500
default_pool_size = 20
INI
echo "\"landiq\" \"${DB_PASSWORD}\"" > /etc/pgbouncer/userlist.txt
systemctl enable --now pgbouncer

# --- Schema migrations -------------------------------------------------------
# Run the project's numbered migrations once. In practice ship migrations/*.sql
# to the instance (S3 sync from the deploy bundle) and apply via psql, e.g.:
#   aws s3 cp s3://${BACKUP_BUCKET}/deploy/migrations/ /opt/landiq/migrations/ --recursive
#   for f in /opt/landiq/migrations/*.sql; do sudo -u postgres psql -d landiq_rag -f "$f"; done

# --- Nightly logical backup to S3 -------------------------------------------
cat > /etc/cron.d/landiq-backup <<CRON
0 17 * * * postgres pg_dump landiq_rag | gzip | aws s3 cp - s3://${BACKUP_BUCKET}/backups/landiq_rag_\$(date +\%Y\%m\%d).sql.gz
CRON
chmod 644 /etc/cron.d/landiq-backup
