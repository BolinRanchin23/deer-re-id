-- Timestamp parsing depends on the session timezone, so PostgreSQL correctly
-- classifies this helper as STABLE rather than IMMUTABLE.
alter function deerid.try_timestamptz(text) stable;
