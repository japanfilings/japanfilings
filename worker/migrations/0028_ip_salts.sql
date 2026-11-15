-- A random key per UTC day for hashing network addresses (sign-in limit, contact form, MCP client
-- registration). Without it a hash of an IPv4 address could be reversed by trying all 2^32 of them.
-- Keys older than two days are deleted, after which no stored hash can be linked to an address.
CREATE TABLE IF NOT EXISTS ip_salts (day TEXT PRIMARY KEY, salt TEXT NOT NULL);
