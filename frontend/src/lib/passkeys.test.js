import { describe, expect, it } from 'vitest';

import { _internals } from './passkeys.js';

const { toBytes, toB64url, creationOptions, requestOptions } = _internals;

/**
 * base64url, not base64. Spec v2.1 §3.2.
 *
 * The two alphabets differ in exactly two characters and base64url has no padding, so the wrong
 * one produces a credential id that looks fine, travels fine, and is never found by the server's
 * lookup. That failure mode is why these are unit tests and not left to the browser: the whole
 * ceremony works and sign-in simply says "that passkey is not registered here".
 */
describe('base64url', () => {
  it('round-trips arbitrary bytes', () => {
    const bytes = new Uint8Array(256).map((_, i) => i);
    expect([...toBytes(toB64url(bytes.buffer))]).toEqual([...bytes]);
  });

  it('emits the url alphabet and no padding', () => {
    // 0xFB 0xFF 0xBE encodes to "+/++" in standard base64 — every character that differs.
    const encoded = toB64url(new Uint8Array([0xfb, 0xff, 0xbe]).buffer);
    expect(encoded).toBe('-_--');
    expect(encoded).not.toMatch(/[+/=]/);
  });

  it('decodes an unpadded value the server sent', () => {
    // py_webauthn strips padding; atob refuses a string whose length is not a multiple of 4,
    // so the decoder has to restore it.
    expect([...toBytes('AQID')]).toEqual([1, 2, 3]);
    expect([...toBytes('AQI')]).toEqual([1, 2]);
    expect([...toBytes('AQ')]).toEqual([1]);
  });

  it('accepts the url alphabet on the way back in', () => {
    expect([...toBytes('-_--')]).toEqual([0xfb, 0xff, 0xbe]);
  });

  it('handles an empty value without throwing', () => {
    expect([...toBytes('')]).toEqual([]);
    expect(toB64url(new Uint8Array([]).buffer)).toBe('');
  });
});

describe('ceremony options', () => {
  const options = {
    challenge: 'AQID',
    rp: { id: 'localhost', name: 'Spielplan' },
    user: { id: 'AQI', name: 'jenny', displayName: 'jenny' },
    excludeCredentials: [{ id: 'AQID', type: 'public-key' }],
    timeout: 60000
  };

  it('turns every binary field into bytes and leaves the rest alone', () => {
    const built = creationOptions(options);
    expect(built.challenge).toBeInstanceOf(Uint8Array);
    expect(built.user.id).toBeInstanceOf(Uint8Array);
    expect(built.excludeCredentials[0].id).toBeInstanceOf(Uint8Array);
    // Not decoded, and not dropped: the authenticator needs them verbatim.
    expect(built.rp).toEqual({ id: 'localhost', name: 'Spielplan' });
    expect(built.timeout).toBe(60000);
    expect(built.user.name).toBe('jenny');
  });

  it('survives a ceremony with no credentials to exclude', () => {
    // The first passkey on an account: py_webauthn omits the key entirely.
    const built = creationOptions({ challenge: 'AQID', user: { id: 'AQI' } });
    expect(built.excludeCredentials).toEqual([]);
  });

  it('converts an authentication ceremony the same way', () => {
    const built = requestOptions({
      challenge: 'AQID',
      allowCredentials: [{ id: '-_--', type: 'public-key' }],
      rpId: 'localhost'
    });
    expect(built.challenge).toBeInstanceOf(Uint8Array);
    expect([...built.allowCredentials[0].id]).toEqual([0xfb, 0xff, 0xbe]);
    expect(built.rpId).toBe('localhost');
  });

  it('survives a discoverable-credential ceremony with no allow list', () => {
    // §3.2's "the phone offers the account itself" path — an unnamed sign-in sends none.
    expect(requestOptions({ challenge: 'AQID' }).allowCredentials).toEqual([]);
  });
});
