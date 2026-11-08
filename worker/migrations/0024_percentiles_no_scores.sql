-- No composite score: percentile facts live in company_indicators.record, and company_facts holds their arithmetic mean.
DROP INDEX IF EXISTS ci_strength;
DROP INDEX IF EXISTS ci_pressure;
ALTER TABLE company_indicators DROP COLUMN strength_score;
ALTER TABLE company_indicators DROP COLUMN pressure_score;
DROP INDEX IF EXISTS cf_strength;
ALTER TABLE company_facts DROP COLUMN strength_score;
ALTER TABLE company_facts DROP COLUMN strength_industry_percentile;
ALTER TABLE company_facts ADD COLUMN mean_percentile REAL;
ALTER TABLE company_facts ADD COLUMN mean_percentile_within_sector REAL;
CREATE INDEX IF NOT EXISTS cf_mean_percentile ON company_facts(mean_percentile, ticker);
