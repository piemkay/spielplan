/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it } from 'vitest';

import AnswerTiles from './AnswerTiles.svelte';

const answers = ['disliked', 'fine', 'liked'].map((answer) => ({ answer, label: answer }));
let app;

afterEach(() => {
  if (app) unmount(app);
  document.body.innerHTML = '';
});

it('flicks the standing answer again on a tap, and still hands the tap on', () => {
  const taps = [];
  app = mount(AnswerTiles, {
    target: document.body,
    props: {
      answers,
      label: 'Your answer',
      pressed: (a) => a.answer === 'liked',
      onAnswer: (a) => taps.push(a.answer)
    }
  });
  const tile = (answer) =>
    /** @type {HTMLElement} */ (document.querySelector(`[data-answer="${answer}"]`));

  tile('liked').click();
  flushSync();
  expect(taps).toEqual(['liked']);
  expect(tile('liked').hasAttribute('data-flick')).toBe(true);

  tile('fine').click();
  flushSync();
  expect(taps).toEqual(['liked', 'fine']);
  expect(document.querySelector('[data-flick]')).toBeNull();
});
