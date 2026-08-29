/**
 * WebAuthn in the browser. Spec v2.1 §3.2 — passkeys are primary.
 *
 * The server speaks the JSON encoding py_webauthn emits (every binary field base64url); the
 * `navigator.credentials` API speaks ArrayBuffers. This module is that translation and
 * nothing else — no policy, no error copy, so the two ends of a ceremony cannot disagree
 * about the wire format in two different files.
 *
 * base64url, not base64: the alphabet differs in two characters and the padding is absent.
 * Getting that wrong produces a credential id the server looks up and never finds.
 */

import { post } from '$lib/api.js';

export const supported = () =>
  typeof window !== 'undefined' && !!window.PublicKeyCredential && !!navigator.credentials;

function toBytes(b64url) {
  const b64 = b64url.replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64.padEnd(Math.ceil(b64.length / 4) * 4, '='));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function toB64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** @param {any} options the server's PublicKeyCredentialCreationOptions JSON */
function creationOptions(options) {
  return {
    ...options,
    challenge: toBytes(options.challenge),
    user: { ...options.user, id: toBytes(options.user.id) },
    excludeCredentials: (options.excludeCredentials ?? []).map((c) => ({
      ...c,
      id: toBytes(c.id)
    }))
  };
}

function requestOptions(options) {
  return {
    ...options,
    challenge: toBytes(options.challenge),
    allowCredentials: (options.allowCredentials ?? []).map((c) => ({ ...c, id: toBytes(c.id) }))
  };
}

/**
 * Register a passkey for the signed-in user. §3.2: multiple per user (phone + desktop), so
 * the label is what tells them apart on the account page a year later.
 */
export async function registerPasskey(label) {
  const { ceremony_id, options } = await post('/auth/passkey/register/options', {});
  const credential = await navigator.credentials.create({
    publicKey: creationOptions(options)
  });
  if (!credential) throw new Error('the passkey prompt was dismissed');
  return post('/auth/passkey/register', {
    ceremony_id,
    label: label || null,
    credential: {
      id: credential.id,
      rawId: toB64url(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: toB64url(credential.response.clientDataJSON),
        attestationObject: toB64url(credential.response.attestationObject),
        transports: credential.response.getTransports?.() ?? []
      },
      clientExtensionResults: credential.getClientExtensionResults?.() ?? {}
    }
  });
}

/**
 * Sign in with a passkey. `name` is optional: with a discoverable credential the phone offers
 * the account itself, which is the whole "Face ID and you are in" experience.
 */
export async function signInWithPasskey(name) {
  const { ceremony_id, options } = await post('/auth/passkey/login/options', {
    name: name || null
  });
  const assertion = await navigator.credentials.get({ publicKey: requestOptions(options) });
  if (!assertion) throw new Error('the passkey prompt was dismissed');
  return post('/auth/passkey/login', {
    ceremony_id,
    device_label: navigator.userAgent,
    credential: {
      id: assertion.id,
      rawId: toB64url(assertion.rawId),
      type: assertion.type,
      response: {
        clientDataJSON: toB64url(assertion.response.clientDataJSON),
        authenticatorData: toB64url(assertion.response.authenticatorData),
        signature: toB64url(assertion.response.signature),
        userHandle: assertion.response.userHandle ? toB64url(assertion.response.userHandle) : null
      },
      clientExtensionResults: assertion.getClientExtensionResults?.() ?? {}
    }
  });
}

export const _internals = { toBytes, toB64url, creationOptions, requestOptions };
