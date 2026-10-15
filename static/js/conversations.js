// Conversation management - sidebar
let currentConversationId = null;
let conversations = [];

async function loadConversations() {
    try {
        const data = await apiGet('/api/conversations');
        conversations = data.conversations;
        renderConversationList();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function renderConversationList() {
    const list = $('#conversation-list');
    list.innerHTML = '';

    if (conversations.length === 0) {
        list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-light);font-size:13px;">No conversations yet</div>';
        return;
    }

    conversations.forEach(conv => {
        const item = document.createElement('div');
        item.className = `conversation-item${conv.id === currentConversationId ? ' active' : ''}`;
        item.innerHTML = `
            <span class="conv-title">${escapeHtml(conv.title)}</span>
            <button class="conv-delete" data-id="${conv.id}" title="Delete">🗑</button>
        `;

        item.addEventListener('click', (e) => {
            if (e.target.closest('.conv-delete')) return;
            selectConversation(conv.id);
        });

        const deleteBtn = item.querySelector('.conv-delete');
        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            deleteConversation(conv.id);
        });

        list.appendChild(item);
    });
}

async function createConversation(title = 'New Chat') {
    try {
        const conv = await apiPost('/api/conversations', { title });
        conversations.unshift(conv);
        renderConversationList();
        selectConversation(conv.id);
        return conv;
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function selectConversation(id) {
    currentConversationId = id;
    renderConversationList();
    await loadMessages(id);
    $('#chat-title').textContent = conversations.find(c => c.id === id)?.title || 'Chat';
    $('#chat-input').focus();
}

async function deleteConversation(id) {
    if (!confirm('Delete this conversation?')) return;

    try {
        await apiDelete(`/api/conversations/${id}`);
        conversations = conversations.filter(c => c.id !== id);
        if (currentConversationId === id) {
            currentConversationId = null;
            $('#chat-messages').innerHTML = '';
            showWelcomeScreen();
            $('#chat-title').textContent = 'AI Chat Agent';
        }
        renderConversationList();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function updateConversationTitle(id, title) {
    try {
        await apiPatch(`/api/conversations/${id}`, { title });
        const conv = conversations.find(c => c.id === id);
        if (conv) conv.title = title;
        renderConversationList();
        if (currentConversationId === id) {
            $('#chat-title').textContent = title;
        }
    } catch (err) {
        showToast(err.message, 'error');
    }
}
