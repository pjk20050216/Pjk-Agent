// API communication layer
const API_BASE = '';

let accessToken = localStorage.getItem('access_token');
let refreshToken = localStorage.getItem('refresh_token');

function setTokens(access, refresh) {
    accessToken = access;
    refreshToken = refresh;
    localStorage.setItem('access_token', access);
    localStorage.setItem('refresh_token', refresh);
}

function clearTokens() {
    accessToken = null;
    refreshToken = null;
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
}

function isAuthenticated() {
    return !!accessToken;
}

async function fetchAPI(endpoint, options = {}) {
    const headers = {
        'Content-Type': 'application/json',
        ...options.headers,
    };

    if (accessToken) {
        headers['Authorization'] = `Bearer ${accessToken}`;
    }

    let response = await fetch(API_BASE + endpoint, {
        ...options,
        headers,
    });

    // Token expired, try refresh
    if (response.status === 401 && refreshToken) {
        const refreshResponse = await fetch(API_BASE + '/api/auth/refresh', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ refresh_token: refreshToken }),
        });

        if (refreshResponse.ok) {
            const data = await refreshResponse.json();
            setTokens(data.access_token, data.refresh_token);
            headers['Authorization'] = `Bearer ${accessToken}`;
            response = await fetch(API_BASE + endpoint, {
                ...options,
                headers,
            });
        } else {
            clearTokens();
            window.dispatchEvent(new CustomEvent('auth-expired'));
            throw new Error('Session expired');
        }
    }

    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Request failed' }));
        throw new Error(error.detail || `HTTP ${response.status}`);
    }

    return response;
}

async function apiGet(endpoint) {
    const response = await fetchAPI(endpoint);
    return response.json();
}

async function apiPost(endpoint, body) {
    const response = await fetchAPI(endpoint, {
        method: 'POST',
        body: JSON.stringify(body),
    });
    return response.json();
}

async function apiPatch(endpoint, body) {
    const response = await fetchAPI(endpoint, {
        method: 'PATCH',
        body: JSON.stringify(body),
    });
    return response.json();
}

async function apiDelete(endpoint) {
    await fetchAPI(endpoint, { method: 'DELETE' });
}

async function apiUpload(endpoint, formData) {
    const headers = {};
    if (accessToken) {
        headers['Authorization'] = `Bearer ${accessToken}`;
    }
    const response = await fetch(API_BASE + endpoint, {
        method: 'POST',
        headers,
        body: formData,
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Upload failed' }));
        throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
}

/**
 * 统一的 SSE 消费器 —— 按「空行分帧」解析，事件类型跨 read() 迭代保持。
 *
 * 注意：eventType 必须声明在读取循环之外。若声明在循环内，一旦某个 SSE 帧的
 * "event:" 行与 "data:" 行被网络分到不同的 chunk（真实网络下几乎必然发生），
 * 下一次迭代会以空 eventType 开始，该帧的 payload 就被静默丢弃 ——
 * 表现为流式回复间歇性空白、done 事件丢失。
 */
async function consumeSSE(response, dispatch) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let eventType = '';

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE 以空行分隔事件帧；\r\n 与 \n 都按换行处理
        let sep;
        while ((sep = buffer.search(/\r?\n\r?\n/)) !== -1) {
            const rawFrame = buffer.slice(0, sep);
            buffer = buffer.slice(sep + buffer.match(/\r?\n\r?\n/)[0].length);

            for (const line of rawFrame.split(/\r?\n/)) {
                if (line.startsWith('event:')) {
                    eventType = line.slice(6).trim();
                } else if (line.startsWith('data:')) {
                    const data = line.slice(5).trimStart();
                    try {
                        dispatch(eventType, JSON.parse(data));
                    } catch (e) {
                        // 流中出现无法解析的行，跳过而不是中断整个响应
                    }
                }
            }
            // 一帧结束，重置事件类型
            eventType = '';
        }
    }
}

// SSE-based streaming chat
async function streamChat(conversationId, content, documentIds, callbacks) {
    const { onToken, onDone, onError } = callbacks;
    const headers = {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${accessToken}`,
    };

    const body = JSON.stringify({
        content: content,
        document_ids: documentIds || [],
    });

    const response = await fetch(`${API_BASE}/api/chat/${conversationId}`, {
        method: 'POST',
        headers,
        body,
    });

    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Chat failed' }));
        throw new Error(error.detail || `HTTP ${response.status}`);
    }

    await consumeSSE(response, (eventType, parsed) => {
        if (eventType === 'token') {
            onToken(parsed.content);
        } else if (eventType === 'done') {
            onDone(parsed);
        } else if (eventType === 'error') {
            onError(parsed.detail);
        }
    });
}

// SSE-based Agent execution
async function streamAgent(request, documentIds, callbacks) {
    const { onPlan, onToolResult, onObserving, onAggregating, onAsking, onFinal, onDone, onError } = callbacks;
    const headers = {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${accessToken}`,
    };

    const body = JSON.stringify({
        request: request,
        document_ids: documentIds || [],
    });

    const response = await fetch(`${API_BASE}/api/agent/run`, {
        method: 'POST',
        headers,
        body,
    });

    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Agent run failed' }));
        throw new Error(error.detail || `HTTP ${response.status}`);
    }

    // 与 streamChat 共用同一套 SSE 解析（原先各自的循环会在帧被分片时丢事件）
    await consumeSSE(response, (eventType, parsed) => {
        switch (eventType) {
            case 'plan': onPlan && onPlan(parsed); break;
            case 'tool_result': onToolResult && onToolResult(parsed); break;
            case 'observing': onObserving && onObserving(parsed); break;
            case 'aggregating': onAggregating && onAggregating(parsed); break;
            case 'asking': onAsking && onAsking(parsed); break;
            case 'final': onFinal && onFinal(parsed); break;
            case 'done': onDone && onDone(parsed); break;
            case 'error': onError && onError(parsed.detail); break;
        }
    });
}
