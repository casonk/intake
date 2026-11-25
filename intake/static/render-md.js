// Render the digest's Markdown source with marked, sanitized by DOMPurify.
// Without scripts the page keeps its server-rendered fallback.
(() => {
  const src = document.getElementById("md-source");
  const out = document.getElementById("md-out");
  if (!src || !out || !window.marked || !window.DOMPurify) return;
  out.innerHTML = DOMPurify.sanitize(marked.parse(JSON.parse(src.textContent), { gfm: true }));
})();
