// Test harness for the /x402-pay page script: runs it under node with a fake DOM, a fake EIP-1193 wallet and a
// fake same-origin fetch. Input (stdin JSON): {html, challenges: {path: PAYMENT-REQUIRED b64}, confirm, chainId,
// clicks: [routeId...], concurrent, receiptHeader}. Output (stdout JSON): what the page did.
"use strict";
const vm = require("vm");
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const html = input.html;
const cfgText = html.split('<script type="application/json" id="cfg">')[1].split("</script>")[0];
const script = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/)[1];
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, innerHTML: "", textContent: id === "cfg" ? cfgText : "", disabled: false, onclick: null };
  return els[id];
}
const out = { signs: [], requests: [], paid: [], confirms: [], wallet: [] };
let chainId = input.chainId || "0x2105";
const ethereum = {
  isMetaMask: true,
  on() {},
  async request({ method, params }) {
    out.wallet.push(method);
    if (method === "eth_accounts" || method === "eth_requestAccounts") return ["0x00000000000000000000000000000000000000aa"];
    if (method === "eth_chainId") return chainId;
    if (method === "eth_getCode") return input.code || "0x";
    if (method === "wallet_switchEthereumChain") { chainId = params[0].chainId; return null; }
    if (method === "eth_signTypedData_v4") { out.signs.push(params); await new Promise(r => setTimeout(r, 20)); return "0x" + "ab".repeat(65); }
    throw new Error("unexpected " + method);
  },
};
function headers(h) { return { get: (k) => { for (const key of Object.keys(h)) if (key.toLowerCase() === k.toLowerCase()) return h[key]; return null; } }; }
async function fetch(path, opts) {
  const sig = opts.headers["PAYMENT-SIGNATURE"];
  out.requests.push({ path, method: opts.method, paid: !!sig, body: JSON.parse(opts.body), credentials: opts.credentials });
  if (sig) {
    out.paid.push({ path, signature: sig });
    const h = input.receiptHeader ? { "PAYMENT-RESPONSE": input.receiptHeader } : {};
    if (input.paidStatus) return { status: input.paidStatus, ok: false, headers: headers({}), json: async () => input.paidBody };
    return { status: 200, ok: true, headers: headers(h), json: async () => (input.paidBody || { status: "ok" }) };
  }
  const c = input.challenges[path];
  return { status: c ? 402 : 500, ok: false, headers: headers(c ? { "PAYMENT-REQUIRED": c } : {}), json: async () => ({}) };
}
const store = {};
const window = { ethereum, addEventListener() {}, dispatchEvent() {} };
const ctx = {
  window, document: { getElementById: el }, fetch, console,
  localStorage: { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = String(v); } },
  confirm: (m) => { out.confirms.push(m); return input.confirm !== false; },
  location: { host: "svc.test", pathname: "/x402-pay", href: "https://svc.test/x402-pay" },
  Event: function (t) { this.type = t; }, setTimeout, TextEncoder, TextDecoder, atob, btoa, crypto: require("crypto").webcrypto,
  BigInt, JSON, Math, Date, Promise, Error, Array, Uint8Array, String, Number, Object,
};
vm.createContext(ctx);
vm.runInContext(script, ctx);
const tick = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
  await tick(300);
  for (const id of Object.keys(els)) if (id.startsWith("price-")) out[id] = els[id].innerHTML;
  out.total = el("total").textContent;
  if (input.concurrent) { const ids = input.clicks; ids.forEach((id) => el("pay-" + id).onclick()); el("pay-" + ids[0]).onclick(); await tick(400); }
  else for (const id of input.clicks || []) { await el("pay-" + id).onclick(); await tick(100); }
  for (const id of Object.keys(els)) if (id.startsWith("res-")) out[id] = els[id].innerHTML;
  out.disabled = Object.fromEntries(Object.keys(els).filter(i => i.startsWith("pay-")).map(i => [i, els[i].disabled]));
  out.store = store;
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
})().catch((e) => { process.stderr.write(String(e.stack || e)); process.exit(1); });
