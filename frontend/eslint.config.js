import eslint from "@eslint/js";
import svelte from "eslint-plugin-svelte";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    // `src/api/generated/**` is orval's deterministic output (never hand-edited,
    // gated by `npm run check:generated`'s empty-diff check instead): it can
    // carry the served OpenAPI document's own markdown escapes verbatim, which
    // is the same reason `sonar-project.properties` excludes this path too.
    ignores: [
      "dist/**",
      "node_modules/**",
      ".svelte-kit/**",
      "src/api/generated/**",
    ],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  ...svelte.configs["flat/recommended"],
  {
    files: ["**/*.svelte"],
    languageOptions: {
      parserOptions: { parser: tseslint.parser },
      globals: {
        window: "readonly",
        sessionStorage: "readonly",
        atob: "readonly",
        TextDecoder: "readonly",
        TextEncoder: "readonly",
        File: "readonly",
        Event: "readonly",
        KeyboardEvent: "readonly",
        HTMLButtonElement: "readonly",
        HTMLDivElement: "readonly",
        HTMLElement: "readonly",
        HTMLInputElement: "readonly",
        CSS: "readonly",
        ResizeObserver: "readonly"
      }
    }
  }
);
