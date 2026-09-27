import { redirect } from '@sveltejs/kit';

// The Data tab split by job (decision 527); its old address lands on Movie data.
export function load() {
  redirect(307, '/admin/movie-data');
}
