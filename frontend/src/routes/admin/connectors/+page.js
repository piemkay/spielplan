import { redirect } from '@sveltejs/kit';

// Connectors split by job (decision 527); its old address lands on Services.
export function load() {
  redirect(307, '/admin/services');
}
