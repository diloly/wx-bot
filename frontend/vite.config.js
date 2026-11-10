import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物直接落到 Flask 能托管的 static/dist/。
// 文件名固定（不带 hash），这样每次 build 只覆盖同名文件，git diff 干净，
// 也不会在 static/dist/assets 里越积越多旧版本。
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
    assetsDir: 'assets',
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: (info) => {
          const name = info.name || ''
          if (name.endsWith('.css')) return 'assets/app.css'
          return 'assets/[name][extname]'
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:5000',
      '/media': 'http://127.0.0.1:5000',
    },
  },
})
