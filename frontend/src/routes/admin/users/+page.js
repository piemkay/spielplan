import { redirect } from '@sveltejs/kit';

// People took the Users tab's place (decision 527).
export function load() {
  redirect(307, '/admin/people');
}
