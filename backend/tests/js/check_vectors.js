// SPDX-License-Identifier: Apache-2.0
// Checks site/js/satoshifi-formats.js against backend/tests/vectors/vectors.json.
'use strict';
const path = require('path');
const fs = require('fs');
const { ethers } = require('ethers');
const F = require(path.join(__dirname, '..', '..', '..', 'site', 'js', 'satoshifi-formats.js'))(ethers);
const V = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'vectors', 'vectors.json'), 'utf8'));
const P = V.params;

let passed = 0;
let failed = 0;
function check(name, fn) {
  try { fn(); passed += 1; console.log('ok   ' + name); }
  catch (e) { failed += 1; console.log('FAIL ' + name + ': ' + e.message); }
}
function eq(actual, expected, what) {
  if (actual !== expected) throw new Error(what + '\n  got:      ' + JSON.stringify(actual) + '\n  expected: ' + JSON.stringify(expected));
}
function signer(message, signature) { return ethers.utils.verifyMessage(message, signature); }
function throws(fn, what) {
  let threw = false;
  try { fn(); } catch (e) { threw = true; }
  if (!threw) throw new Error('expected rejection: ' + what);
}

V.canon.forEach((c) => check('canon ' + c.name, () => {
  eq(F.jcs(c.value), c.jcs, 'jcs');
  eq(F.keccakText(c.jcs), c.keccak, 'keccak');
}));
check('canon rejects float', () => throws(() => F.jcs({ x: 1.5 }), 'float'));
check('canon rejects unsafe int', () => throws(() => F.jcs({ x: 2 ** 53 }), 'unsafe int'));
check('canon rejects lone surrogate', () => throws(() => F.jcs({ x: '\ud800' }), 'surrogate'));

check('request', () => {
  const r = V.request;
  eq(F.requestHash(r.body), r.hash, 'hash');
  eq(F.requestMessage(P.domain, P.chain_id, r.issued_at, r.body), r.message, 'message');
  eq(signer(r.message, r.signature), r.signer, 'signer');
});

check('wsig', () => {
  const w = V.wsig;
  eq(F.wsigMessage(w.deployment, P.chain_id, w.work, w.code, w.exp), w.message, 'message');
  eq(signer(w.message, w.signature), w.signer, 'signer');
});
check('wsig rejects lowercase work address', () => throws(() => F.wsigMessage('s13', 1, V.wsig.work.toLowerCase(), V.wsig.code, 1), 'lowercase'));

check('binding SIWE', () => {
  const b = V.binding_siwe; const w = V.wsig;
  eq(F.wsigResource(w.signature), b.resource, 'resource');
  const msg = F.siweMessage({
    domain: P.domain, address: b.signer, statement: F.bindingStatement(w.deployment, w.work),
    uri: P.uri, chainId: P.chain_id, nonce: w.code, issuedAt: P.t0, expiration: w.exp, resources: [b.resource]
  });
  eq(msg, b.message, 'message');
  eq(signer(b.message, b.signature), b.signer, 'signer');
});

check('login SIWE', () => {
  const s = V.login_siwe;
  const msg = F.siweMessage({
    domain: P.domain, address: s.signer, statement: F.loginStatement(), uri: P.uri,
    chainId: P.chain_id, nonce: s.nonce, issuedAt: P.t0, expiration: P.t0 + 300
  });
  eq(msg, s.message, 'message');
  eq(signer(s.message, s.signature), s.signer, 'signer');
});

V.api.forEach((a) => check('api ' + a.method + ' ' + a.path, () => {
  eq(F.apiString(a.method, a.path, a.timestamp, a.nonce, a.body), a.message, 'message');
  eq(signer(a.message, a.signature), a.signer, 'signer');
}));
check('api rejects bad nonce', () => throws(() => F.apiString('GET', '/v1/x', 1, '0x00', ''), 'nonce'));

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed ? 1 : 0);
