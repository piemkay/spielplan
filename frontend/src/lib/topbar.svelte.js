// A surface that owns its first row (Rate) hands the shell a snippet for the top row, beside You.
// Without a shell to host it (a component mounted alone), the surface draws the row itself.
export const topbar = $state({ content: null, host: false });
