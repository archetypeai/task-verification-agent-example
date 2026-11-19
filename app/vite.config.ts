import tailwindcss from '@tailwindcss/vite';
import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},
			// A static single-page app, served by server/server.py.
			adapter: adapter({ fallback: 'index.html' })
		})
	],
	server: {
		// `npm run dev` talks to the Python API on its default port.
		proxy: { '/api': 'http://127.0.0.1:8765' }
	}
});
