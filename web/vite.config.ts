import { defineConfig } from "vite";

export default defineConfig({
  server: {
    fs: { allow: [".."] },
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
