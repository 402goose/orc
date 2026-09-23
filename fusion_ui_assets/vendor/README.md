# Browser dependencies

Vendored for offline use; users do not need Node or a package manager.
Exact npm versions, source filenames, and package integrity hashes are recorded
in `manifest.json`. Upstream licenses are included next to each bundle.

- [Marked](https://github.com/markedjs/marked): Markdown and GFM tables.
- [DOMPurify](https://github.com/cure53/DOMPurify): sanitizes rendered Markdown.

When updating, use `npm pack PACKAGE@VERSION --ignore-scripts`, copy the listed
browser bundle and upstream license, update the manifest, and run the browser
checks. Keep raw HTML restricted by the allowlist in `app.js`; Marked alone does
not sanitize content. The server's content policy also blocks remote resources.
