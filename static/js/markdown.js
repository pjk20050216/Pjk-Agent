// Markdown rendering with marked.js + highlight.js
marked.setOptions({
    highlight: function (code, lang) {
        if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
        }
        return hljs.highlightAuto(code).value;
    },
    breaks: true,
    gfm: true,
});

const renderer = new marked.Renderer();

renderer.code = function ({ text, lang }) {
    const langLabel = lang || '';
    const highlighted = lang && hljs.getLanguage(lang)
        ? hljs.highlight(text, { language: lang }).value
        : hljs.highlightAuto(text).value;
    return `
        <div class="code-block-wrapper">
            <div style="display:flex;align-items:center;justify-content:space-between;background:#181825;padding:6px 16px;border-radius:8px 8px 0 0;font-size:12px;color:#a6adc8;">
                <span>${escapeHtml(langLabel)}</span>
                <button class="copy-btn" onclick="copyCode(this)" style="position:static;opacity:1;">Copy</button>
            </div>
            <pre style="margin-top:0;border-radius:0 0 8px 8px;"><code class="hljs language-${escapeHtml(langLabel)}">${highlighted}</code></pre>
        </div>`;
};

marked.use({ renderer });

function renderMarkdown(text) {
    return marked.parse(text);
}

function copyCode(btn) {
    const wrapper = btn.closest('.code-block-wrapper');
    const code = wrapper.querySelector('code');
    const text = code ? code.textContent : '';
    navigator.clipboard.writeText(text).then(() => {
        btn.textContent = 'Copied!';
        setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
    }).catch(() => {
        btn.textContent = 'Failed';
        setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
    });
}
