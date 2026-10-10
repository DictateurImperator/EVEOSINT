-- Economic facts are deduplicated across the overlapping histories in MERs.
-- Newer report months take precedence. Values keep PostgreSQL NUMERIC precision.
CREATE SCHEMA IF NOT EXISTS mer;

CREATE TABLE IF NOT EXISTS mer.economy_imports (
    report_month date PRIMARY KEY,
    archive_path text NOT NULL,
    economic_fingerprint text NOT NULL,
    parser_version integer NOT NULL,
    status text NOT NULL CHECK (status IN ('running','success','failed')),
    manifest jsonb NOT NULL DEFAULT '[]',
    rows_read bigint NOT NULL DEFAULT 0,
    facts_written bigint NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    error text
);

CREATE TABLE IF NOT EXISTS mer.region_economy_monthly (
    period_start date NOT NULL,
    scope_kind text NOT NULL CHECK (scope_kind IN ('region','region_name','space_group','wormhole_class')),
    scope_key text NOT NULL,
    scope_name text NOT NULL,
    region_id bigint,
    dataset text NOT NULL,
    metric text NOT NULL,
    dimensions jsonb NOT NULL DEFAULT '{}',
    value numeric NOT NULL,
    unit text NOT NULL,
    source_month date NOT NULL,
    source_member text NOT NULL,
    PRIMARY KEY (period_start,scope_kind,scope_key,dataset,metric,dimensions)
);

CREATE TABLE IF NOT EXISTS mer.global_economy_history (
    period_start date NOT NULL,
    period_grain text NOT NULL CHECK (period_grain IN ('day','month')),
    dataset text NOT NULL,
    metric text NOT NULL,
    dimensions jsonb NOT NULL DEFAULT '{}',
    value numeric NOT NULL,
    unit text NOT NULL,
    source_month date NOT NULL,
    source_member text NOT NULL,
    PRIMARY KEY (period_start,period_grain,dataset,metric,dimensions)
);

CREATE TABLE IF NOT EXISTS mer.isk_flow_history (
    LIKE mer.global_economy_history INCLUDING ALL
);

-- Future/unrecognized CSVs and columns are preserved, never silently discarded.
CREATE TABLE IF NOT EXISTS mer.economy_unmapped_rows (
    source_month date NOT NULL,
    source_member text NOT NULL,
    source_row bigint NOT NULL,
    reason text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (source_month,source_member,source_row)
);

CREATE INDEX IF NOT EXISTS region_economy_region_month
    ON mer.region_economy_monthly(region_id,period_start);
CREATE INDEX IF NOT EXISTS global_economy_series
    ON mer.global_economy_history(dataset,metric,period_start);
CREATE INDEX IF NOT EXISTS isk_flow_series
    ON mer.isk_flow_history(dataset,metric,period_start);

CREATE OR REPLACE VIEW mer.region_economy_evolution AS
SELECT r.*,
       r.value - p.value AS monthly_change,
       100 * (r.value / NULLIF(p.value,0) - 1) AS monthly_change_pct,
       100 * (r.value / NULLIF(y.value,0) - 1) AS yearly_change_pct
FROM mer.region_economy_monthly r
LEFT JOIN mer.region_economy_monthly p
  ON p.period_start = (r.period_start - interval '1 month')::date
 AND (p.scope_kind,p.scope_key,p.dataset,p.metric,p.dimensions,p.unit)
   = (r.scope_kind,r.scope_key,r.dataset,r.metric,r.dimensions,r.unit)
LEFT JOIN mer.region_economy_monthly y
  ON y.period_start = (r.period_start - interval '1 year')::date
 AND (y.scope_kind,y.scope_key,y.dataset,y.metric,y.dimensions,y.unit)
   = (r.scope_kind,r.scope_key,r.dataset,r.metric,r.dimensions,r.unit);

-- Beginning/end mean first/last published observation, not an invented balance
-- at midnight. Daily volatility uses consecutive-day relative changes only.
CREATE OR REPLACE VIEW mer.global_economy_monthly AS
WITH daily AS (
 SELECT h.*,
        lag(value) OVER w AS previous_value,
        lag(period_start) OVER w AS previous_date
 FROM mer.global_economy_history h
 WINDOW w AS (PARTITION BY period_grain,dataset,metric,dimensions,unit ORDER BY period_start)
), monthly AS (
 SELECT date_trunc('month',period_start)::date AS month,
        period_grain,dataset,metric,dimensions,unit,
        min(period_start) AS first_observation_date,
        max(period_start) AS last_observation_date,
        (array_agg(value ORDER BY period_start))[1] AS first_value,
        (array_agg(value ORDER BY period_start DESC))[1] AS last_value,
        avg(value) AS mean_value, count(*) AS observations,
        stddev_samp(CASE WHEN period_grain='day' AND period_start-previous_date=1
                        THEN 100*(value/NULLIF(previous_value,0)-1) END) AS daily_return_volatility_pct
 FROM daily GROUP BY 1,2,3,4,5,6
)
SELECT m.*, m.last_value-m.first_value AS within_month_change,
       avg(m.last_value) OVER (PARTITION BY m.period_grain,m.dataset,m.metric,m.dimensions,m.unit
                              ORDER BY m.month RANGE BETWEEN interval '2 months' PRECEDING AND CURRENT ROW) AS moving_average_3_months,
       100*(m.last_value/NULLIF(p.last_value,0)-1) AS monthly_change_pct,
       100*(m.last_value/NULLIF(y.last_value,0)-1) AS yearly_change_pct
FROM monthly m
LEFT JOIN monthly p ON p.month=(m.month-interval '1 month')::date
 AND (p.period_grain,p.dataset,p.metric,p.dimensions,p.unit)=(m.period_grain,m.dataset,m.metric,m.dimensions,m.unit)
LEFT JOIN monthly y ON y.month=(m.month-interval '1 year')::date
 AND (y.period_grain,y.dataset,y.metric,y.dimensions,y.unit)=(m.period_grain,m.dataset,m.metric,m.dimensions,m.unit);

-- Preserve CCP's signed sinks; summing daily/monthly publications separately
-- avoids adding the same flow twice when both formats are available.
CREATE OR REPLACE VIEW mer.isk_flow_monthly AS
SELECT date_trunc('month',period_start)::date AS month,
       period_grain,dataset,metric,dimensions,unit,
       sum(value) AS value, count(*) AS observations
FROM mer.isk_flow_history
GROUP BY 1,2,3,4,5,6;
