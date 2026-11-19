<script lang="ts">
	import './layout.css';
	import favicon from '#lib/assets/favicon.svg';
	import { Toaster } from '@archetypeai/ds-ui-svelte-console/primitives/sonner';
	import { darkMode } from '@archetypeai/ds-ui-svelte-console/primitives/theme';
	import type { LayoutProps } from './$types';

	let { children }: LayoutProps = $props();

	$effect(() => {
		darkMode.set(window.matchMedia('(prefers-color-scheme: dark)').matches);
	});
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<title>Task Verification</title>
</svelte:head>

<Toaster />
{@render children()}
