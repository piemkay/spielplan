// One toast at a time, above the tab bar: what just happened, with its undo (decision 527).
export const toast = $state({ id: 0, message: '', actionLabel: '', action: null });

let timer = null;

/** @param {string} message @param {{ label: string, run: () => void } | null} [action] */
export function showToast(message, action = null, ms = 5000) {
  toast.id += 1;
  toast.message = message;
  toast.actionLabel = action?.label ?? '';
  toast.action = action?.run ?? null;
  clearTimeout(timer);
  timer = setTimeout(hideToast, ms);
}

export function hideToast() {
  clearTimeout(timer);
  toast.message = '';
  toast.action = null;
  toast.actionLabel = '';
}
