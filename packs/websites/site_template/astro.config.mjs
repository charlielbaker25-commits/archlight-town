// The Builder may not edit this file (the Town checks its hash before every build).
// A plain static site: no server, no integrations, nothing that phones home.
import { defineConfig } from "astro/config";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  output: "static",
  devToolbar: { enabled: false },
  build: { inlineStylesheets: "always" },
  vite: { plugins: [tailwindcss()] },
});
