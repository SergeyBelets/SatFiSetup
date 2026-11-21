// SPDX-License-Identifier: Apache-2.0
// Signed-text formats for create-confederation.html (wrapper spec, section 5.3).
// Must produce byte-identical output to backend/src/satoshifi/common/messages.py;
// backend/tests/js/check_vectors.js checks this against the shared vectors.
// Requires ethers 5.7.2 as the global `ethers` (browser) or passed in (Node).
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory;
  } else {
    root.SatoshiFiFormats = factory(root.ethers);
  }
})(typeof self !== 'undefined' ? self : this, function (ethers) {
  'use strict';

  if (!ethers || !ethers.utils) {
    throw new Error('SatoshiFiFormats: ethers 5.7.2 is required');
  }
  var u = ethers.utils;

  var DEPLOYMENT_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  var CODE_RE = /^[0-9a-f]{64}$/;
  var API_NONCE_RE = /^0x[0-9a-f]{32}$/;
  var DOMAIN_RE = /^[a-z0-9.-]+(:[0-9]{1,5})?$/;
  var METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];

  function fail(message) {
    throw new Error('SatoshiFiFormats: ' + message);
  }

  function requireChecksum(address, what) {
    if (typeof address !== 'string' || !/^0x[0-9a-fA-F]{40}$/.test(address) || u.getAddress(address) !== address) {
      fail((what || 'address') + ' must be a checksum address');
    }
    return address;
  }

  function requireUnix(value, what) {
    if (!Number.isSafeInteger(value) || value < 0) fail(what + ' must be a non-negative integer');
    return value;
  }

  function requireChainId(value) {
    if (!Number.isSafeInteger(value) || value <= 0) fail('chain_id must be a positive integer');
    return value;
  }

  function requireMatch(re, value, what) {
    if (typeof value !== 'string' || !re.test(value)) fail('bad ' + what);
    return value;
  }

  // RFC 8785 for the API value set: objects, arrays, strings, booleans, null, safe integers.
  function jcs(value) {
    if (value === null) return 'null';
    if (value === true) return 'true';
    if (value === false) return 'false';
    if (typeof value === 'number') {
      if (!Number.isSafeInteger(value)) fail('only safe integers are allowed');
      return String(value);
    }
    if (typeof value === 'string') {
      if (/[\uD800-\uDFFF]/.test(value.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g, ''))) fail('lone surrogate in string');
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) return '[' + value.map(jcs).join(',') + ']';
    if (typeof value === 'object') {
      var keys = Object.keys(value).sort();
      return '{' + keys.map(function (k) { return jcs(k) + ':' + jcs(value[k]); }).join(',') + '}';
    }
    fail('unsupported type: ' + typeof value);
  }

  function keccakText(text) {
    return u.keccak256(u.toUtf8Bytes(text));
  }

  function rfc3339(unix) {
    return new Date(requireUnix(unix, 'time') * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
  }

  function requestHash(body) {
    return keccakText(jcs(body));
  }

  function requestMessage(domain, chainId, issuedAt, body) {
    requireMatch(DOMAIN_RE, domain, 'domain');
    requireChainId(chainId);
    requireUnix(issuedAt, 'issued_at');
    return [
      'SatoshiFi confederation request v1',
      'Domain: ' + domain,
      'Chain ID: ' + chainId,
      'Issued at: ' + issuedAt,
      'Request hash: ' + requestHash(body)
    ].join('\n');
  }

  function wsigMessage(deployment, chainId, work, code, exp) {
    requireMatch(DEPLOYMENT_RE, deployment, 'deployment');
    requireChainId(chainId);
    requireChecksum(work, 'work address');
    requireMatch(CODE_RE, code, 'code');
    requireUnix(exp, 'exp');
    return [
      'SatoshiFi work binding v1',
      'Deployment: ' + deployment,
      'Chain ID: ' + chainId,
      'Work address: ' + work,
      'Code: ' + code,
      'Expires: ' + exp
    ].join('\n');
  }

  function wsigResource(wsigSignature) {
    var sig = String(wsigSignature).toLowerCase();
    if (!/^0x[0-9a-f]{130}$/.test(sig)) fail('bad wsig signature');
    return 'urn:satoshifi:wsig:' + u.keccak256(sig);
  }

  function loginStatement() {
    return 'Sign in to SatoshiFi.';
  }

  function bindingStatement(deployment, work) {
    requireMatch(DEPLOYMENT_RE, deployment, 'deployment');
    requireChecksum(work, 'work address');
    return 'My work address for ' + deployment + ' is ' + work + '.';
  }

  // f: {domain, address, statement, uri, chainId, nonce, issuedAt, expiration, resources}
  function siweMessage(f) {
    requireMatch(DOMAIN_RE, f.domain, 'domain');
    requireChecksum(f.address);
    if (typeof f.statement !== 'string' || !f.statement || f.statement.indexOf('\n') >= 0) fail('bad statement');
    if (typeof f.uri !== 'string' || f.uri.indexOf('https://') !== 0 || /\s/.test(f.uri)) fail('uri must be an https URL without spaces');
    requireChainId(f.chainId);
    if (typeof f.nonce !== 'string' || !/^[A-Za-z0-9]{8,}$/.test(f.nonce)) fail('bad nonce');
    var lines = [
      f.domain + ' wants you to sign in with your Ethereum account:',
      f.address,
      '',
      f.statement,
      '',
      'URI: ' + f.uri,
      'Version: 1',
      'Chain ID: ' + f.chainId,
      'Nonce: ' + f.nonce,
      'Issued At: ' + rfc3339(f.issuedAt),
      'Expiration Time: ' + rfc3339(f.expiration)
    ];
    var resources = f.resources || [];
    if (resources.length) {
      lines.push('Resources:');
      resources.forEach(function (r) {
        if (typeof r !== 'string' || !r || /\s/.test(r)) fail('bad resource');
        lines.push('- ' + r);
      });
    }
    return lines.join('\n');
  }

  // bodyText: the exact string sent as the HTTP body ('' for GET).
  function apiString(method, path, timestamp, nonce, bodyText) {
    if (METHODS.indexOf(method) < 0) fail('bad method');
    if (typeof path !== 'string' || path.indexOf('/v1/') !== 0 || /\s/.test(path)) fail('bad path');
    requireUnix(timestamp, 'timestamp');
    requireMatch(API_NONCE_RE, nonce, 'nonce');
    if (typeof bodyText !== 'string') fail('body must be a string');
    return ['SatoshiFi api v1', method + ' ' + path, String(timestamp), nonce, keccakText(bodyText)].join('\n');
  }

  return {
    jcs: jcs,
    keccakText: keccakText,
    rfc3339: rfc3339,
    requireChecksum: requireChecksum,
    requestHash: requestHash,
    requestMessage: requestMessage,
    wsigMessage: wsigMessage,
    wsigResource: wsigResource,
    loginStatement: loginStatement,
    bindingStatement: bindingStatement,
    siweMessage: siweMessage,
    apiString: apiString
  };
});
