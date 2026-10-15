// Chat UI logic - messages, streaming, input
let isStreaming = false;
let abortController = null;
let isAgentMode = false;
let agentRunId = null;

function showWelcomeScreen() {
    const container = $('#chat-messages');
    container.innerHTML = `
        <div class="welcome-screen" id="welcome-screen">
            <div class="welcome-icon">🤖</div>
            <h2>Hello! How can I help you today?</h2>
            <p>Start a conversation or select one from the sidebar</p>
            <div class="suggestions">
                <button class="suggestion-chip" data-text="Explain quantum computing in simple terms">Explain quantum computing</button>
                <button class="suggestion-chip" data-text="Write a Python script to sort a list">Write Python sorting code</button>
                <button class="suggestion-chip" data-text="Help me write a professional email">Help write an email</button>
                <button class="suggestion-chip" data-text="What are the best practices for REST API design?">REST API best practices</button>
            </div>
        </div>`;
    initSuggestionChips();
}

async function loadMessages(conversationId) {
    try {
        const data = await apiGet(`/api/conversations/${conversationId}`);
        const container = $('#chat-messages');
        container.innerHTML = '';

        if (data.messages.length === 0) {
            showWelcomeScreen();
            return;
        }

        data.messages.forEach(msg => {
            appendMessage(msg.role, msg.content);
        });
        scrollToBottom();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function appendMessage(role, content) {
    const container = $('#chat-messages');
    const welcomeScreen = $('#welcome-screen');
    if (welcomeScreen) welcomeScreen.remove();

    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}`;

    const avatar = role === 'user' ? '👤' : '🤖';

    let renderedContent;
    if (role === 'assistant') {
        try {
            renderedContent = renderMarkdown(content);
        } catch (e) {
            renderedContent = escapeHtml(content);
        }
    } else {
        renderedContent = escapeHtml(content);
    }

    msgDiv.innerHTML = `
        <div class="message-avatar">${avatar}</div>
        <div class="message-content">${renderedContent}</div>
    `;

    container.appendChild(msgDiv);
    scrollToBottom();
}

function appendStreamingMessage() {
    const container = $('#chat-messages');
    const welcomeScreen = $('#welcome-screen');
    if (welcomeScreen) welcomeScreen.remove();

    const msgDiv = document.createElement('div');
    msgDiv.className = 'message assistant';
    msgDiv.id = 'streaming-message';
    msgDiv.innerHTML = `
        <div class="message-avatar">🤖</div>
        <div class="message-content"></div>
    `;
    container.appendChild(msgDiv);
    scrollToBottom();
}

function updateStreamingContent(text) {
    const contentEl = document.querySelector('#streaming-message .message-content');
    if (contentEl) {
        try {
            contentEl.innerHTML = renderMarkdown(text);
        } catch (e) {
            contentEl.textContent = text;
        }
        scrollToBottom();
    }
}

function finishStreaming(messageId, conversationId) {
    const streamingMsg = $('#streaming-message');
    if (streamingMsg) {
        streamingMsg.removeAttribute('id');
    }
    removeStopButton();
    isStreaming = false;

    // Update conversation list order
    if (currentConversationId === conversationId) {
        const conv = conversations.find(c => c.id === conversationId);
        if (conv) {
            conversations = [conv, ...conversations.filter(c => c.id !== conversationId)];
            renderConversationList();
        }
    }
}

function showStopButton() {
    removeStopButton();
    const btn = document.createElement('button');
    btn.className = 'stop-btn';
    btn.textContent = '■ Stop generating';
    btn.id = 'stop-btn';
    btn.addEventListener('click', stopGeneration);
    $('#chat-messages').appendChild(btn);
}

function removeStopButton() {
    const btn = $('#stop-btn');
    if (btn) btn.remove();
}

function stopGeneration() {
    if (abortController) {
        abortController.abort();
        abortController = null;
    }
    finishStreaming();
    removeStopButton();
    isStreaming = false;
}

async function sendMessage() {
    if (isStreaming) return;

    const input = $('#chat-input');
    const content = input.value.trim();
    if (!content) return;

    if (isAgentMode) {
        await sendAgentMessage(content);
        return;
    }

    // Auto-create conversation if none selected
    if (!currentConversationId) {
        const conv = await createConversation(content.substring(0, 50));
        if (!conv) return;
        currentConversationId = conv.id;
    }

    // Show user message
    appendMessage('user', content);
    input.value = '';
    input.style.height = 'auto';
    updateSendButton();

    // Show streaming placeholder
    appendStreamingMessage();
    showStopButton();

    // Start streaming
    isStreaming = true;
    let fullContent = '';

    try {
        await streamChat(
            currentConversationId,
            content,
            attachedDocIds,
            {
                onToken: (token) => {
                    fullContent += token;
                    updateStreamingContent(fullContent);
                },
                onDone: (data) => {
                    finishStreaming(data.message_id, data.conversation_id);
                },
                onError: (error) => {
                    showToast(error, 'error');
                    finishStreaming();
                },
            }
        );
    } catch (err) {
        if (err.name !== 'AbortError') {
            showToast(err.message, 'error');
        }
        finishStreaming();
    }

    isStreaming = false;
    removeStopButton();
}

// ----------- Agent Mode -----------

function toggleAgentMode() {
    isAgentMode = !isAgentMode;
    const btn = $('#agent-mode-toggle');
    const input = $('#chat-input');

    if (isAgentMode) {
        btn.classList.add('active');
        btn.textContent = '🤖 Agent Mode (ON)';
        input.placeholder = '描述你的需求，Agent 将自动规划并执行...';
        showToast('Agent 模式已开启 — 我会自动调用工具完成复杂任务', 'info');
    } else {
        btn.classList.remove('active');
        btn.textContent = '🤖 Agent Mode';
        input.placeholder = 'Type your message...';
    }
}

async function sendAgentMessage(content) {
    const input = $('#chat-input');
    input.value = '';
    input.style.height = 'auto';
    updateSendButton();

    // Show user message
    appendMessage('user', content);

    // Show agent progress container
    const agentMsgId = appendAgentProgress();
    showStopButton();
    isStreaming = true;

    try {
        await streamAgent(
            content,
            attachedDocIds,
            {
                onPlan: (data) => {
                    updateAgentStep(agentMsgId, 'plan', data);
                },
                onToolResult: (data) => {
                    updateAgentStep(agentMsgId, 'tool_result', data);
                },
                onObserving: (data) => {
                    updateAgentStep(agentMsgId, 'observing', data);
                },
                onAggregating: (data) => {
                    updateAgentStep(agentMsgId, 'aggregating', data);
                },
                onAsking: (data) => {
                    showAgentQuestions(agentMsgId, data);
                    finishStreaming();
                },
                onFinal: (data) => {
                    showAgentFinal(agentMsgId, data);
                    agentRunId = data.agent_run_id;
                },
                onDone: (data) => {
                    finishStreaming(data.agent_run_id, null);
                },
                onError: (error) => {
                    showToast(error, 'error');
                    finishStreaming();
                },
            }
        );
    } catch (err) {
        if (err.name !== 'AbortError') {
            showToast(err.message, 'error');
        }
        finishStreaming();
    }

    isStreaming = false;
    removeStopButton();
}

function appendAgentProgress() {
    const container = $('#chat-messages');
    const welcomeScreen = $('#welcome-screen');
    if (welcomeScreen) welcomeScreen.remove();

    const msgId = 'agent-msg-' + Date.now();
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message assistant agent-message';
    msgDiv.id = msgId;
    msgDiv.innerHTML = `
        <div class="message-avatar">🤖</div>
        <div class="message-content">
            <div class="agent-progress">
                <div class="agent-status-line">
                    <span class="agent-spinner"></span>
                    <span class="agent-status-text">正在规划任务...</span>
                </div>
                <div class="agent-steps"></div>
            </div>
        </div>
    `;
    container.appendChild(msgDiv);
    scrollToBottom();
    return msgId;
}

function updateAgentStep(msgId, eventType, data) {
    const msgDiv = document.getElementById(msgId);
    if (!msgDiv) return;

    const statusText = msgDiv.querySelector('.agent-status-text');
    const stepsContainer = msgDiv.querySelector('.agent-steps');

    switch (eventType) {
        case 'plan':
            statusText.textContent = `规划完成 — 共 ${data.steps?.length || 0} 个步骤`;
            if (data.steps) {
                stepsContainer.innerHTML = data.steps.map((s, i) =>
                    `<div class="agent-step pending" data-step="${i}">
                        <span class="step-icon">⏳</span>
                        <span class="step-name">${escapeHtml(s.tool)}</span>
                        <span class="step-reason">${escapeHtml(s.reason || '')}</span>
                    </div>`
                ).join('');
            }
            break;
        case 'tool_result':
            statusText.textContent = `执行: ${data.tool || data.step}`;
            // Mark step as done
            const stepIdx = data.step ? parseInt(data.step.split('_')[1]) : null;
            if (stepIdx !== null && stepsContainer) {
                const stepEl = stepsContainer.querySelector(`[data-step="${stepIdx}"]`);
                if (stepEl) {
                    stepEl.classList.remove('pending');
                    stepEl.classList.add(data.success ? 'done' : 'error');
                    stepEl.querySelector('.step-icon').textContent = data.success ? '✅' : '❌';
                }
            }
            // Add summary
            if (data.summary) {
                const summaryDiv = document.createElement('div');
                summaryDiv.className = 'agent-step-summary';
                summaryDiv.textContent = data.summary;
                stepsContainer.appendChild(summaryDiv);
            }
            break;
        case 'aggregating':
            statusText.textContent = '正在生成最终报告...';
            break;
    }
    scrollToBottom();
}

function showAgentQuestions(msgId, data) {
    const msgDiv = document.getElementById(msgId);
    if (!msgDiv) return;

    const content = msgDiv.querySelector('.message-content');
    content.innerHTML = `
        <div class="agent-questions">
            <div class="agent-questions-icon">❓</div>
            <p><strong>Agent 需要确认以下信息：</strong></p>
            <ul>
                ${(data.questions || []).map(q => `<li>${escapeHtml(q)}</li>`).join('')}
            </ul>
            <p class="agent-questions-hint">请根据以上问题重新输入您的需求，Agent 将继续执行</p>
        </div>
    `;
    scrollToBottom();
}

function showAgentFinal(msgId, data) {
    const msgDiv = document.getElementById(msgId);
    if (!msgDiv) return;

    const content = msgDiv.querySelector('.message-content');
    const renderedContent = renderMarkdown(data.content);
    content.innerHTML = `
        <div class="agent-final">
            <div class="agent-final-header">📋 执行完成</div>
            <div class="agent-final-content">${renderedContent}</div>
        </div>
    `;
    scrollToBottom();
}

function scrollToBottom() {
    const container = $('#chat-messages');
    requestAnimationFrame(() => {
        container.scrollTop = container.scrollHeight;
    });
}

function updateSendButton() {
    const input = $('#chat-input');
    const btn = $('#send-btn');
    btn.disabled = !input.value.trim() || isStreaming;
}

function initChatInput() {
    const input = $('#chat-input');
    const btn = $('#send-btn');

    input.addEventListener('input', () => {
        updateSendButton();
        // Auto-resize
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 150) + 'px';
    });

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    btn.addEventListener('click', sendMessage);

    // Agent mode toggle
    const agentToggle = $('#agent-mode-toggle');
    if (agentToggle) {
        agentToggle.addEventListener('click', toggleAgentMode);
    }

    $('#new-chat-btn').addEventListener('click', () => {
        if (isStreaming) return;
        currentConversationId = null;
        $('#chat-messages').innerHTML = '';
        showWelcomeScreen();
        $('#chat-title').textContent = 'AI Chat Agent';
        attachedDocIds = [];
        updateAttachedDocs();
        renderConversationList();
    });

    $('#logout-btn').addEventListener('click', () => {
        clearTokens();
        location.reload();
    });

    // Mobile sidebar toggle
    $('#sidebar-toggle').addEventListener('click', () => {
        $('#sidebar').classList.toggle('open');
    });
}

function initSuggestionChips() {
    const chips = $$('.suggestion-chip');
    chips.forEach(chip => {
        chip.addEventListener('click', () => {
            const text = chip.dataset.text;
            $('#chat-input').value = text;
            updateSendButton();
            $('#chat-input').focus();
            sendMessage();
        });
    });
}
