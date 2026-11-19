import js from '@eslint/js';
import svelte from 'eslint-plugin-svelte';
import globals from 'globals';
import ts from 'typescript-eslint';

export default ts.config(
	js.configs.recommended,
	...ts.configs.recommended,
	...svelte.configs.recommended,
	...svelte.configs['flat/prettier'],
	{
		languageOptions: {
			globals: { ...globals.browser, ...globals.node }
		}
	},
	{
		files: ['**/*.svelte', '**/*.svelte.js', '**/*.svelte.ts'],
		languageOptions: {
			parserOptions: { parser: ts.parser }
		}
	},
	{
		// registry components use plain hrefs by design
		files: ['src/lib/components/**'],
		rules: { 'svelte/no-navigation-without-resolve': 'off' }
	},
	{
		ignores: ['.svelte-kit/', 'build/', 'dist/', 'node_modules/']
	}
);
