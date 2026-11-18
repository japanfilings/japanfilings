// Unit tests for the sign-in limit's network key. No server needed.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test, { after } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(mkdtempSync(join(tmpdir(), "jf-")), "m.mjs");
after(() => rmSync(dirname(out), { recursive: true, force: true }));
writeFileSync(out + ".entry.ts", `export { networkOf } from "${here}/../src/accounts";`);
await build({ entryPoints: [out + ".entry.ts"], bundle: true, format: "esm", outfile: out, platform: "neutral", logLevel: "error", external: ["hono"] });
const { networkOf } = await import(pathToFileURL(out));

test("IPv4 is counted per address", () => {
  assert.equal(networkOf("203.0.113.7"), "203.0.113.7");
});

test("IPv6 is counted per /64, however the address is written", () => {
  const n = "2001:db8:1:2::/64";
  for (const ip of ["2001:db8:1:2::1", "2001:db8:1:2:abcd:ef01:2345:6789", "2001:0db8:0001:0002:0:0:0:ff", "2001:DB8:1:2::"]) assert.equal(networkOf(ip), n, ip);
  assert.notEqual(networkOf("2001:db8:1:3::1"), n);
  assert.equal(networkOf("2001:db8::1"), "2001:db8:0:0::/64");
  assert.equal(networkOf("::1"), "0:0:0:0::/64");
});
