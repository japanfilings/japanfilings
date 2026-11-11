-- Sign-in links are limited per network as well as per address (hash of the address of the request, changes daily).
ALTER TABLE login_tokens ADD COLUMN ip_hash TEXT;
CREATE INDEX IF NOT EXISTS login_tokens_ip ON login_tokens (ip_hash, expires_at);
