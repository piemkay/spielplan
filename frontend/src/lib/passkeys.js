// Translates py_webauthn's base64url JSON to and from navigator.credentials' ArrayBuffers.

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

export async function registerPasskey(label) {
  const { ceremony_id, options } = await post('/auth/passkey/register/options', {});
  // One cast per ceremony: lib.dom types the result as a bare `Credential`.
  const credential = /** @type {any} */ (
    await navigator.credentials.create({ publicKey: creationOptions(options) })
  );
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

// `name` is optional: with a discoverable credential the phone offers the account itself.
export async function signInWithPasskey(name) {
  const { ceremony_id, options } = await post('/auth/passkey/login/options', {
    name: name || null
  });
  const assertion = /** @type {any} */ (
    await navigator.credentials.get({ publicKey: requestOptions(options) })
  );
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
