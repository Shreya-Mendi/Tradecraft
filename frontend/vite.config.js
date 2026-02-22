import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');

  return {
    root: '.',                // frontend/ is the root
    base: '/',                // served from repo root on GitHub Pages

    // Replace placeholder strings in index.html with env values at build time
    define: {
      __VITE_GH_OWNER__: JSON.stringify(env.VITE_GH_OWNER ?? ''),
      __VITE_GH_REPO__:  JSON.stringify(env.VITE_GH_REPO  ?? ''),
    },

    build: {
      outDir: 'dist',
      emptyOutDir: true,
      sourcemap: false,        // keep build lean for Pages
      rollupOptions: {
        input: {
          main: './index.html',
        },
        output: {
          // Stable chunk names for caching
          chunkFileNames: 'assets/[name]-[hash].js',
          entryFileNames: 'assets/[name]-[hash].js',
          assetFileNames: 'assets/[name]-[hash][extname]',
        },
      },
    },

    server: {
      port: 5173,
      open: true,
    },
  };
});
