import { readFileSync } from "node:fs";
import worker, { _crypto } from "../relay/mail_worker.js";

let passed = 0;
function ok(condition, message) {
  if (!condition) throw new Error(message);
  passed += 1;
}

class KV {
  constructor() { this.rows = new Map(); }
  async get(key, type) {
    const raw = this.rows.get(key);
    if (raw == null) return null;
    return type === "json" ? JSON.parse(raw) : raw;
  }
  async put(key, value) { this.rows.set(key, value); }
  async delete(key) { this.rows.delete(key); }
}

const directory = new KV();
const env = { DIRECTORY: directory };
const pub = _crypto.b64(crypto.getRandomValues(new Uint8Array(32)));
const code = "731942";
directory.rows.set("handle:rowan", JSON.stringify({
  pub, code_hash: await _crypto.verificationDigest("rowan", pub, code), attempts: 0,
  expires_at: Math.floor(Date.now() / 1000) + 1200, verified: false,
}));

async function verify(handle, candidate) {
  return worker.fetch(new Request("https://mail.collie.run/handle/verify", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ handle, pub, code: candidate }),
  }), env);
}

for (let i = 0; i < 4; i += 1) {
  const response = await verify("rowan", "000000");
  ok(response.status === 401, "a wrong code before the limit must be rejected");
}
const locked = await verify("rowan", "000000");
ok(locked.status === 429, "the fifth wrong code must lock the claim");
const lockedCorrect = await verify("rowan", code);
ok(lockedCorrect.status === 429, "a locked claim must not accept a later correct guess");

directory.rows.set("handle:juno", JSON.stringify({
  pub, code_hash: await _crypto.verificationDigest("juno", pub, code), attempts: 0,
  expires_at: Math.floor(Date.now() / 1000) + 1200, verified: false,
}));
const verified = await verify("juno", code);
ok(verified.status === 200, "a fresh matching claim should verify");
const durable = JSON.parse(directory.rows.get("handle:juno"));
ok(durable.verified === true && !!durable.verified_at, "verified authority is durable");
ok(!("code" in durable) && !("code_hash" in durable) && !("email" in durable),
   "verified authority must retain neither bootstrap email nor verification material");

const source = readFileSync(new URL("../relay/mail_worker.js", import.meta.url), "utf8");
ok(!source.includes("Math.random()"), "mail verification codes must use WebCrypto CSPRNG");
ok(/^\d{6}$/.test(_crypto.randomDigits(6)), "generated claims are fixed-width decimal codes");

console.log(`mail claim security: ${passed}/${passed} passed`);
