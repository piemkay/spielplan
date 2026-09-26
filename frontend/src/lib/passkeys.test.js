import { describe, expect, it } from 'vitest';

import { _internals } from './passkeys.js';

const { toBytes, toB64url, creationOptions, requestOptions } = _internals;

// The wrong alphabet yields a credential id the server's lookup silently never finds.
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
    // py_webauthn strips padding, and atob refuses a length that is not a multiple of 4.
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
    // Not decoded and not dropped: the authenticator needs them verbatim.
    expect(built.rp).toEqual({ id: 'localhost', name: 'Spielplan' });
    expect(built.timeout).toBe(60000);
    expect(built.user.name).toBe('jenny');
  });

  it('survives a ceremony with no credentials to exclude', () => {
    // py_webauthn omits the key entirely for an account's first passkey.
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
    expect(requestOptions({ challenge: 'AQID' }).allowCredentials).toEqual([]);
  });
});
