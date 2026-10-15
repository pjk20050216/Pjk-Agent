// Document management
let documents = [];
let attachedDocIds = [];

async function loadDocuments() {
    try {
        const data = await apiGet('/api/documents');
        documents = data.documents;
        renderDocumentList();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function renderDocumentList() {
    const list = $('#document-list');
    if (!list) return;

    if (documents.length === 0) {
        list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-light);font-size:13px;">No documents uploaded</div>';
        return;
    }

    const hint = `<div class="doc-list-hint">点击文档可附加到对话，用于 RAG 检索</div>`;

    list.innerHTML = hint + documents.map(doc => {
        const selected = attachedDocIds.includes(doc.id);
        // 仅 ready 的文档有向量索引，其余附加后检索不到内容
        const selectable = doc.status === 'ready';
        const cls = ['doc-item'];
        if (selected) cls.push('selected');
        if (!selectable) cls.push('not-selectable');
        return `
        <div class="${cls.join(' ')}" data-id="${doc.id}">
            <span class="doc-check">${selected ? '✓' : ''}</span>
            <span class="doc-icon">${doc.file_type === 'pdf' ? '📄' : '📝'}</span>
            <div class="doc-info">
                <div class="doc-name">${escapeHtml(doc.original_filename)}</div>
                <div class="doc-meta">${formatBytes(doc.file_size)} · ${doc.chunk_count} chunks</div>
            </div>
            <span class="doc-status ${doc.status}">${doc.status}</span>
            <button class="doc-delete" data-id="${doc.id}" title="Delete">🗑</button>
        </div>
    `;
    }).join('');

    list.querySelectorAll('.doc-delete').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();   // 不要触发所在行的「附加」切换
            await deleteDocument(parseInt(btn.dataset.id));
        });
    });

    list.querySelectorAll('.doc-item').forEach(item => {
        item.addEventListener('click', (e) => {
            if (e.target.closest('.doc-delete')) return;
            toggleAttachDocument(parseInt(item.dataset.id));
        });
    });
}

async function uploadFile(file) {
    const formData = new FormData();
    formData.append('file', file);

    const progress = $('#upload-progress');
    progress.classList.remove('hidden');

    try {
        const doc = await apiUpload('/api/documents/upload', formData);
        documents.unshift(doc);
        renderDocumentList();
        showToast('File uploaded successfully', 'success');
        updateAttachedDocs();
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        progress.classList.add('hidden');
    }
}

async function deleteDocument(id) {
    try {
        await apiDelete(`/api/documents/${id}`);
        documents = documents.filter(d => d.id !== id);
        attachedDocIds = attachedDocIds.filter(did => did !== id);
        renderDocumentList();
        updateAttachedDocs();
        showToast('Document deleted', 'success');
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function toggleAttachDocument(docId) {
    const doc = documents.find(d => d.id === docId);
    if (!doc) return;

    // 只有 ready 的文档建立了向量索引；附加 processing/error 的文档
    // 只会在检索时落空，用户却以为已经生效，故直接拒绝并说明原因。
    if (doc.status !== 'ready') {
        showToast(`「${doc.original_filename}」尚未索引完成（${doc.status}），无法附加`, 'error');
        return;
    }

    const idx = attachedDocIds.indexOf(docId);
    if (idx >= 0) {
        attachedDocIds.splice(idx, 1);
    } else {
        attachedDocIds.push(docId);
    }
    renderDocumentList();
    updateAttachedDocs();
}

function updateAttachedDocs() {
    const container = $('#attached-docs');
    if (!container) return;

    const attached = documents.filter(d => attachedDocIds.includes(d.id));
    container.innerHTML = attached.map(d => `
        <span class="doc-tag">
            ${escapeHtml(d.original_filename.substring(0, 20))}${d.original_filename.length > 20 ? '...' : ''}
            <button class="doc-tag-remove" data-id="${d.id}">×</button>
        </span>
    `).join('');

    container.querySelectorAll('.doc-tag-remove').forEach(btn => {
        btn.addEventListener('click', () => {
            attachedDocIds = attachedDocIds.filter(id => id !== parseInt(btn.dataset.id));
            renderDocumentList();   // 同步文档列表里的勾选状态
            updateAttachedDocs();
        });
    });
}

function initUploadModal() {
    const modal = $('#upload-modal');
    const uploadZone = $('#upload-zone');
    const fileInput = $('#file-input');
    const closeBtn = modal.querySelector('.modal-close');

    $('#upload-doc-btn').addEventListener('click', () => {
        modal.classList.remove('hidden');
        loadDocuments();
    });

    closeBtn.addEventListener('click', () => modal.classList.add('hidden'));
    modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.classList.add('hidden');
    });

    uploadZone.addEventListener('click', () => fileInput.click());
    uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadZone.classList.add('drag-over');
    });
    uploadZone.addEventListener('dragleave', () => {
        uploadZone.classList.remove('drag-over');
    });
    uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');
        const files = e.dataTransfer.files;
        if (files.length > 0) uploadFile(files[0]);
    });

    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            uploadFile(fileInput.files[0]);
            fileInput.value = '';
        }
    });

    renderDocumentList();
}
