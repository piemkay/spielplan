import { afterEach, describe, expect, it, vi } from 'vitest';

import { flipFrom, ms, still } from './motion.js';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('motion', () => {
  it('is still wherever the preference cannot be read, so node and jsdom start no motion timer', () => {
    expect(still()).toBe(true);
    expect(ms(220)).toBe(0);
  });

  it('moves only while the person has no preference against it', () => {
    let reduce = false;
    vi.stubGlobal('matchMedia', (query) => ({
      matches: query === '(prefers-reduced-motion: no-preference)' && !reduce
    }));
    expect(still()).toBe(false);
    expect(ms(220)).toBe(220);
    reduce = true;
    expect(still()).toBe(true);
    expect(ms(220)).toBe(0);
  });

  it('glides an element from where it was, and not at all when still', () => {
    const animate = vi.fn();
    const el = { getBoundingClientRect: () => ({ left: 10, top: 20 }), animate };
    flipFrom(el, { left: 40, top: 20 }, 240);
    expect(animate).not.toHaveBeenCalled();
    vi.stubGlobal('matchMedia', () => ({ matches: true }));
    flipFrom(el, { left: 40, top: 20 }, 240);
    expect(animate.mock.calls[0][0][0].transform).toBe('translate(30px, 0px)');
    expect(animate.mock.calls[0][1].duration).toBe(240);
  });
});
