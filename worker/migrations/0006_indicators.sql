-- Company indicators (financial strength, policy uncertainty, ownership pressure) and the monthly index.
CREATE TABLE IF NOT EXISTS company_indicators (
  ticker TEXT PRIMARY KEY,
  fiscal_year_end TEXT,
  doc_id TEXT,
  strength_score REAL,
  policy_uncertainty REAL,
  pressure_score REAL,
  record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ci_strength ON company_indicators(strength_score);
CREATE INDEX IF NOT EXISTS ci_pu ON company_indicators(policy_uncertainty);
CREATE INDEX IF NOT EXISTS ci_pressure ON company_indicators(pressure_score);
CREATE TABLE IF NOT EXISTS policy_uncertainty_monthly (
  month TEXT PRIMARY KEY,
  record TEXT NOT NULL
);
