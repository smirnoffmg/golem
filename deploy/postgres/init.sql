-- One cluster, one database per owner. No service role can reach another owner's database.
-- Passwords below are placeholders for local development; real ones come from the secret store.

\set ON_ERROR_STOP on

CREATE ROLE golem_tasks LOGIN PASSWORD 'dev-only-golem-tasks' CONNECTION LIMIT 40;
CREATE ROLE golem_runs  LOGIN PASSWORD 'dev-only-golem-runs'  CONNECTION LIMIT 20;
CREATE ROLE golem_edge  LOGIN PASSWORD 'dev-only-golem-edge'  CONNECTION LIMIT 10;
CREATE ROLE golem_mcp   LOGIN PASSWORD 'dev-only-golem-mcp'   CONNECTION LIMIT 10;
-- Owns the audit log but is not any service's identity, so no running service holds
-- UPDATE, DELETE or TRUNCATE on it.
CREATE ROLE golem_audit_owner NOLOGIN;

ALTER ROLE golem_tasks SET statement_timeout = '5s';
ALTER ROLE golem_runs  SET statement_timeout = '5s';
ALTER ROLE golem_edge  SET statement_timeout = '2s';
ALTER ROLE golem_mcp   SET statement_timeout = '2s';

CREATE DATABASE golem_tasks OWNER golem_tasks;
CREATE DATABASE golem_runs  OWNER golem_runs;
CREATE DATABASE golem_audit OWNER golem_audit_owner;

REVOKE CONNECT, TEMPORARY ON DATABASE golem_tasks FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE golem_runs  FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE golem_audit FROM PUBLIC;

GRANT CONNECT ON DATABASE golem_audit TO golem_edge, golem_mcp;

\connect golem_audit

SET ROLE golem_audit_owner;

CREATE TABLE audit_log (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    account       text        NOT NULL,
    request       text        NOT NULL,
    target_system text        NOT NULL,
    operation     text        NOT NULL,
    result        text        NOT NULL,
    rows_affected bigint,
    source        text        NOT NULL,
    source_ip     inet,
    chain         text[]      NOT NULL DEFAULT '{}'
);

REVOKE ALL ON audit_log FROM PUBLIC;
GRANT INSERT ON audit_log TO golem_edge, golem_mcp;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO golem_edge, golem_mcp;

RESET ROLE;
