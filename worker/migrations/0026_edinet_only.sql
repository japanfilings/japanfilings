-- Data read from company websites is no longer provided.
-- earnings_releases keeps only rows read from EDINET filings (url edinet:<doc_id>).
DROP TABLE IF EXISTS call_documents;
DROP TABLE IF EXISTS ir_status;
DELETE FROM earnings_releases WHERE url NOT LIKE 'edinet:%';
DELETE FROM dataset_status WHERE dataset IN ('calls', 'earnings_releases');
