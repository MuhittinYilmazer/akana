import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const MD_PATH = path.join(REPO, "web_ui/static/akana-markdown.js");

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const ctx = { window: { AkanaCore: { escapeHtml } }, console };
vm.runInNewContext(readFileSync(MD_PATH, "utf8"), ctx);
const { render, preprocess } = ctx.window.AkanaMarkdown;

assert.match(render("**merhaba** dünya"), /<strong>merhaba<\/strong>/);
assert.match(render("- bir\n- iki"), /<ul>[\s\S]*<li>bir<\/li>[\s\S]*<li>iki<\/li>/);
assert.match(render("Başlık:\nbir şey\nbaşka şey"), /<ul>[\s\S]*<li>bir şey<\/li>/);
assert.match(render("## Alt başlık"), /<h2 class="md-h md-h2">/);
assert.match(render("[site](https://example.com)"), /<a class="md-link" href="https:\/\/example.com"/);
assert.match(render("```js\nconst x = 1;\n```"), /<pre class="md-code"/);
assert.match(render("> alıntı"), /<blockquote class="md-quote">/);
assert.match(render("- [ ] yap\n- [x] bitti"), /md-task-list/);

// E1: markdown markers INSIDE an inline code span must stay literal — `a*b*c` must not
// become a<em>b</em>c, and a URL inside backticks must not be auto-linked.
const codeStar = render("use `a*b*c` and `**x**` now");
assert.match(codeStar, /<code class="md-inline-code">a\*b\*c<\/code>/);
assert.match(codeStar, /<code class="md-inline-code">\*\*x\*\*<\/code>/);
assert.doesNotMatch(codeStar, /<em>|<strong>/);
const codeUrl = render("run `curl https://example.com/x`");
assert.match(codeUrl, /<code class="md-inline-code">curl https:\/\/example\.com\/x<\/code>/);
assert.doesNotMatch(codeUrl, /<a class="md-link"/);
// emphasis OUTSIDE code still works alongside a code span.
const mixed = render("**bold** and `code` and *em*");
assert.match(mixed, /<strong>bold<\/strong>/);
assert.match(mixed, /<em>em<\/em>/);
assert.match(mixed, /<code class="md-inline-code">code<\/code>/);

const streamed = render("```\npartial", { streaming: true });
assert.match(streamed, /md-code--partial/);

const pre = preprocess("Satır:\nMadde bir\nMadde iki");
assert.match(pre, /- Madde bir/);

console.log("akana_markdown.harness: ok");
