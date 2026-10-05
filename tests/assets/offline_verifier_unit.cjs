/* Actual runtime + Node WebCrypto. No claim of browser, storage or hardware coverage. */
"use strict";
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
const config = JSON.parse(fs.readFileSync(0, "utf8"));
config.enabled = true;
Object.defineProperty(globalThis, "crypto", { value: webcrypto });
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
globalThis.window = globalThis;
globalThis.BroadcastChannel = undefined;
globalThis.document = {
  readyState: "loading", addEventListener() {}, cookie: "",
  getElementById(id) { return id === "asc-offline-config" ? { textContent: JSON.stringify(config) } : null; },
  querySelectorAll() { return []; },
};
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));

async function main() {
  const a = globalThis.AscOffline;
  const now = Math.floor(Date.now() / 1000);
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  const spki = a.b64u(await crypto.subtle.exportKey("spki", pair.publicKey));
  const claims = { v: 1, cv: 1, eid: "SYNTHETIC", jti: "J".repeat(22), pid: "P".repeat(22),
    bai: "B".repeat(22), n: "N".repeat(16), apc: "GENERAL", btc: "STANDARD", nbf: now - 60, exp: now + 3600 };
  async function signed(changes = {}, key = pair.privateKey) {
    const header = a.b64u(new TextEncoder().encode(a.canonical({alg:"ES256",kid:"v1",typ:"ASC-PASS"})));
    const payload = a.b64u(new TextEncoder().encode(a.canonical({...claims, ...changes})));
    const signature = await crypto.subtle.sign({name:"ECDSA",hash:"SHA-256"},key,new TextEncoder().encode(header+"."+payload));
    return header+"."+payload+"."+a.b64u(signature);
  }
  const entry = { jti:claims.jti,pid:claims.pid,bai:claims.bai,cv:1,apc:claims.apc,btc:claims.btc,
    valid_from:claims.nbf,valid_until:claims.exp,display_name:"Synthetic Participant",
    badge_label:{en:"Standard",fr:"Standard",ar:"Standard"},assignment_until:null,
    profile_window:{from:null,until:null},reentry:"SINGLE_ENTRY",
    zone_rules:[{zone:"MAIN",rules:[{effect:"ALLOW",from:null,until:null}]}],restrictions:[],last_admitted_at:null };
  const body = { schema_version:1,package_id:"Q".repeat(22),package_version:1,device:"D".repeat(22),event:"SYNTHETIC",
    gate:"GA",zones:["MAIN"],scope_version:1,sensitivity:"STANDARD",issued_at:now-10,data_cutoff_at:now-10,
    aging_at:now+900,stale_at:now+1800,expires_at:now+3600,entries:[entry],revoked_passes:[],
    qr_keys:[{kid:"v1",spki,status:"ACTIVE",not_before:null,not_after:null}],override_reasons:[] };
  function reset() {
    a.state.body = structuredClone(body); a.state.activeMeta = {...body};
    a.state.device = {public_id:body.device}; a.state.offline = {available:true,prepared:true,ready:true};
    a.state.grants = [{schema:2,grant_id:"G".repeat(22),device:body.device,event:body.event,gate:"GA",zone:"MAIN",scope_version:1,
      issued_at:now-5,expires_at:now+1800,permissions:["verify","admit"],locked:false}];
    a.state.index=null; a.state.deltaBody=null; a.state.deltaMeta=null; a.state.qrKeys={};
    a.state.rebuildReasons=[]; a.state.rebuildRequired=false; a.state.localAdmissions={};
    a.state.storageError=false; a.state.directives.block=false;
    a.health.mode="offline"; a.health.lastActivity=Date.now(); a.health.clockDiscontinuity=false;
  }
  const checks = [];
  globalThis.currentCheck = "initialization";
  async function check(name, fn) { globalThis.currentCheck=name; reset(); await fn(); checks.push(name); }
  const qr = await signed();
  await check("valid ES256 QR", async () => assert.equal((await a.verifyOfflineQr(qr)).result,"ALLOWED"));
  await check("malformed QR", async () => assert.equal((await a.verifyOfflineQr("not-a-qr")).code,"MALFORMED"));
  await check("invalid signature", async () => {
    const other=await crypto.subtle.generateKey({name:"ECDSA",namedCurve:"P-256"},false,["sign","verify"]);
    assert.equal((await a.verifyOfflineQr(await signed({},other.privateKey))).code,"INVALID_SIGNATURE");
  });
  await check("wrong event", async () => assert.equal((await a.verifyOfflineQr(await signed({eid:"OTHER"}))).reason,"WRONG_EVENT"));
  await check("unsupported version", async () => assert.equal((await a.verifyOfflineQr(await signed({v:99}))).code,"UNSUPPORTED_VERSION"));
  await check("signed local miss", async () => assert.equal((await a.verifyOfflineQr(await signed({jti:"X".repeat(22)}))).reason,"OFFLINE_LOCAL_MISS"));
  for (const status of ["REVOKED","REPLACED","SUSPENDED"]) {
    await check("known "+status, async () => {
      a.state.body.revoked_passes=[{jti:claims.jti,status}];
      assert.equal((await a.verifyOfflineQr(qr)).code,status);
      assert.notEqual((await a.verifyOfflineQr(qr)).result,"ALLOWED");
    });
  }
  await check("wrong zone", async () => {a.state.body.entries[0].zone_rules=[];assert.equal((await a.verifyOfflineQr(qr)).reason,"WRONG_ZONE");});
  await check("stale", async () => {a.state.activeMeta.stale_at=now-1;assert.equal((await a.verifyOfflineQr(qr)).result,"MANUAL_REVIEW");});
  await check("expired", async () => {a.state.activeMeta.expires_at=now-1;await assert.rejects(a.verifyOfflineQr(qr),{code:"STATE_CHANGED"});});
  await check("blocking delta", async () => {a.state.rebuildReasons=["SCOPE_CHANGED"];await assert.rejects(a.verifyOfflineQr(qr),{code:"STATE_CHANGED"});});
  await check("wrong checkpoint grant", async () => {a.state.grants[0].gate="GB";await assert.rejects(a.verifyOfflineQr(qr),{code:"STATE_CHANGED"});});
  await check("local anti-passback", async () => {a.state.localAdmissions[claims.jti]=now-1;assert.equal((await a.verifyOfflineQr(qr)).reason,"ALREADY_ADMITTED");});
  await check("delta index does not outlive its package", async () => {
    function delta(passes) {return {delta_version:1,passes,restrictions:[],access_withdrawn:[],withdrawn_access_profiles:[],revoked_kids:[]};}
    a.state.deltaBody=delta([{jti:claims.jti,status:"REVOKED"}]);
    assert.equal((await a.verifyOfflineQr(qr)).code,"REVOKED");
    a.state.body={...a.state.body,package_id:"R".repeat(22)};
    a.state.deltaBody=delta([]);
    assert.equal((await a.verifyOfflineQr(qr)).code,"VALID");
  });
  await check("health debounce and recovery", async () => {
    a.health.mode="online";
    assert.equal(a.recordHealth(false,0),"online");
    assert.equal(a.recordHealth(false,10000),"online");
    assert.equal(a.recordHealth(false,20000),"offline");
    assert.equal(a.recordHealth(true,21000),"offline");
    assert.equal(a.recordHealth(true,31000),"recovering");
  });
  await check("no unsafe lock fallback", async () => {
    await assert.rejects(a.opstore.append(null,{}),{code:"STORAGE_UNAVAILABLE"});
  });
  process.stdout.write(JSON.stringify({passed:checks.length,checks,scope:"Node WebCrypto unit coverage only"}));
}
main().catch((e) => { process.stderr.write("Offline verifier unit assertion failed: "+globalThis.currentCheck+" "+e.name+" "+(e.code || "")+"\n"); process.stderr.write(String(e.stack).split("\n").slice(1,4).join("\n")); process.exitCode=1; });
