import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist"] },
  js.configs.recommended,
  ...tseslint.configs.strict,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { globals: globals.browser },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "no-restricted-syntax": [
        "error",
        {
          // Nothing from a task or a proposal is ever rendered as HTML (ADR 0018).
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: "Render payloads as text; the board never injects HTML.",
        },
        {
          // The CSP has no 'unsafe-inline' for styles: CSS files only.
          selector: "JSXAttribute[name.name='style']",
          message: "Use a class from the stylesheet; the CSP blocks inline styles.",
        },
      ],
    },
  },
);
