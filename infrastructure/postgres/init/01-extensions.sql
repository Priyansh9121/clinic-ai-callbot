-- Runs once, on first initialisation of the Docker Postgres volume.
-- Kept minimal: the schema itself belongs to Alembic, not to init scripts.

-- Used for gen_random_uuid() should a future migration want database-side ids.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- The application role must not be able to create databases or roles. It is
-- created as the POSTGRES_USER by the image, so tighten it here.
ALTER ROLE clinic_app NOCREATEDB NOCREATEROLE NOSUPERUSER;

-- No implicit access for any other role.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO clinic_app;
