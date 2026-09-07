import { describe, expect, it } from 'vitest';

import { authMethodLine } from './session.svelte.js';

/**
 * fe-13: the account chip printed the constant 'passkey + PIN' to everybody, so §3.2's example
 * string was rendered as a claim about credentials a brand-new member did not have.
 */
describe('authMethodLine', () => {
  it('names the password when there is no passkey, because §3.2 always keeps one', () => {
    // Not the empty string: an account with neither a passkey nor a PIN still has a password.
    expect(authMethodLine({ passkeys: 0, has_pin: false })).toBe('password');
  });

  it('names the PIN only when one is set', () => {
    expect(authMethodLine({ passkeys: 0, has_pin: true })).toBe('password + PIN');
  });

  it('reproduces the example in §3.2 for the account that has both', () => {
    expect(authMethodLine({ passkeys: 2, has_pin: true })).toBe('passkey + PIN');
  });

  it('drops the password once a passkey exists, because §3.2 makes passkeys primary', () => {
    expect(authMethodLine({ passkeys: 1, has_pin: false })).toBe('passkey');
  });

  it('reads a missing count as no passkey rather than as NaN', () => {
    // `/auth/me` carries both fields, but the chip renders during bootstrap from whatever
    // `session.user` holds at that instant.
    expect(authMethodLine({})).toBe('password');
  });

  it('says nothing at all when nobody is signed in', () => {
    expect(authMethodLine(null)).toBe('');
  });
});
