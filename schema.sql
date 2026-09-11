CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date date PRIMARY KEY,
    blocks_filename text NOT NULL,
    asns_filename text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);

-- Temporal rows use a half-open interval: [valid_from, valid_until).
-- NULL valid_until means that the version is current.
CREATE TABLE IF NOT EXISTS route_versions (
    prefix cidr NOT NULL,
    asn bigint NOT NULL CHECK (asn >= 0),
    valid_from date NOT NULL REFERENCES snapshots(snapshot_date),
    valid_until date REFERENCES snapshots(snapshot_date),
    PRIMARY KEY (prefix, asn, valid_from),
    CHECK (valid_until IS NULL OR valid_until > valid_from)
);

CREATE TABLE IF NOT EXISTS asn_versions (
    asn bigint NOT NULL CHECK (asn >= 0),
    name text NOT NULL,
    class text,
    country_code varchar(2),
    valid_from date NOT NULL REFERENCES snapshots(snapshot_date),
    valid_until date REFERENCES snapshots(snapshot_date),
    PRIMARY KEY (asn, valid_from),
    CHECK (valid_until IS NULL OR valid_until > valid_from)
);

-- This is itself a delta table. Empty old/new arrays represent additions and
-- withdrawals. It powers the recent-changes page without replaying history.
CREATE TABLE IF NOT EXISTS prefix_changes (
    snapshot_date date NOT NULL REFERENCES snapshots(snapshot_date) ON DELETE CASCADE,
    prefix cidr NOT NULL,
    old_asns bigint[] NOT NULL,
    new_asns bigint[] NOT NULL,
    PRIMARY KEY (snapshot_date, prefix)
);

CREATE UNIQUE INDEX IF NOT EXISTS route_versions_current_idx
    ON route_versions (prefix, asn) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS route_versions_asn_period_idx
    ON route_versions (asn, valid_from, valid_until);
CREATE INDEX IF NOT EXISTS route_versions_prefix_gist_idx
    ON route_versions USING gist (prefix inet_ops);
CREATE UNIQUE INDEX IF NOT EXISTS asn_versions_current_idx
    ON asn_versions (asn) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS prefix_changes_date_idx
    ON prefix_changes (snapshot_date DESC, prefix);
