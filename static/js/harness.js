/* Agent Harness 控制台 —— 无构建依赖的原生 JS 单页。
 * 数据流：POST /runs 创建 -> GET /runs/{id}/events (SSE) 实时更新任务板与时间线
 *        -> 终态后拉取报告 / 文件 / 追踪 / 审批。
 */
const HX = {
    runs: [],
    runId: null,
    run: null,
    events: [],
    lastSeq: 0,
    streamAbort: null,
    approvals: [],
    skills: [],
    activeTab: 'approvals',
    booted: false,
};

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

const STATUS_LABEL = {
    queued: '排队中',
    planning: '规划中',
    running: '执行中',
    waiting_approval: '待审批',
    paused: '已暂停',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    pending: '待执行',
    in_progress: '进行中',
    blocked: '阻塞',
    skipped: '跳过',
};

const AGENT_LABEL = {
    feature_analysis: '特征分析',
    rule_config: '规则配置',
    test_validation: '测试验证',
    anomaly_diagnosis: '异常排查',
    knowledge_retrieval: '知识检索',
    data_collection: '数据采集',
};

function esc(value) {
    return escapeHtml(String(value == null ? '' : value));
}

function statusLabel(status) {
    return STATUS_LABEL[status] || status || '未知';
}

function isTerminal(status) {
    return TERMINAL.has(status);
}

function fmtTime(iso) {
    if (!iso) return '--:--:--';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '--:--:--';
    return d.toLocaleTimeString('zh-CN', { hour12: false });
}

function fmtDateTime(iso) {
    if (!iso) return '-';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '-';
    return d.toLocaleString('zh-CN', { hour12: false });
}

function fmtDuration(ms) {
    if (ms == null) return '-';
    if (ms < 1000) return `${ms} ms`;
    return `${(ms / 1000).toFixed(1)} s`;
}

function md(text) {
    if (!text) return '';
    try {
        if (typeof renderMarkdown === 'function') return renderMarkdown(text);
    } catch (err) {
        console.warn('markdown render failed', err);
    }
    return `<pre>${esc(text)}</pre>`;
}

function pill(label, value, tone) {
    const cls = tone ? ` ${tone}` : '';
    return `<span class="hx-pill${cls}"><span class="hx-dot"></span>${esc(label)} <b>${esc(value)}</b></span>`;
}

function stateBadge(status) {
    return `<span class="hx-state ${esc(status)}">${esc(statusLabel(status))}</span>`;
}

/* ------------------------------------------------------------------ 启动 */
document.addEventListener('DOMContentLoaded', () => {
    bindStatic();
    if (isAuthenticated()) {
        startApp();
    } else {
        showLogin();
    }
});

window.addEventListener('auth-expired', showLogin);

function showLogin() {
    if (HX.streamAbort) {
        HX.streamAbort.abort();
        HX.streamAbort = null;
    }
    HX.booted = false;
    $('#hx-login').classList.remove('hidden');
    $('#hx-app').classList.add('hidden');
}

async function startApp() {
    $('#hx-login').classList.add('hidden');
    $('#hx-app').classList.remove('hidden');
    if (HX.booted) return;
    HX.booted = true;
    try {
        await Promise.all([loadStatus(), loadRuns(), loadSkills(), loadApprovals()]);
        const first = HX.runs[0];
        if (first) await selectRun(first.id);
    } catch (err) {
        showToast(err.message || '初始化失败', 'error');
    }
}

function bindStatic() {
    const loginForm = $('#hx-login-form');
    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const username = $('#hx-login-username').value.trim();
        const password = $('#hx-login-password').value;
        const errorEl = $('#hx-login-error');
        errorEl.textContent = '';
        try {
            const data = await apiPost('/api/auth/login', { username, password });
            setTokens(data.access_token, data.refresh_token);
            HX.booted = false;
            await startApp();
        } catch (err) {
            errorEl.textContent = err.message || '登录失败';
        }
    });

    $('#hx-new-run').addEventListener('click', () => {
        $('#hx-modal-new').classList.remove('hidden');
        $('#hx-objective').focus();
    });

    $('#hx-create-run').addEventListener('click', createRun);

    $('#hx-refresh').addEventListener('click', async () => {
        await Promise.all([loadStatus(), loadRuns(), loadApprovals()]);
        if (HX.runId) await refreshRun();
        showToast('已刷新', 'success');
    });

    $$('.hx-chip').forEach((chip) => {
        chip.addEventListener('click', () => {
            $('#hx-objective').value = chip.dataset.sample || '';
            $('#hx-objective').focus();
        });
    });

    $('#hx-open-report').addEventListener('click', () => {
        if (!HX.run || !HX.run.result || !HX.run.result.report_path) return;
        openFile(HX.run.result.report_path);
    });

    $('#hx-memory-search').addEventListener('click', searchMemory);
    $('#hx-memory-q').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') searchMemory();
    });

    document.addEventListener('click', (e) => {
        const closer = e.target.closest('[data-close]');
        if (closer) {
            const id = closer.dataset.close;
            const el = document.getElementById(id);
            if (el) el.classList.add('hidden');
            return;
        }
        if (e.target.id === 'hx-modal' || e.target.id === 'hx-modal-new') {
            e.target.classList.add('hidden');
            return;
        }
        const tab = e.target.closest('.hx-tab');
        if (tab) {
            activateTab(tab.dataset.tab);
            return;
        }
        // 文件链接可能嵌在任务卡里，必须先于 [data-task] 判断
        const fileEl = e.target.closest('[data-file]');
        if (fileEl) {
            e.preventDefault();
            openFile(fileEl.dataset.file);
            return;
        }
        const taskEl = e.target.closest('[data-task]');
        if (taskEl) {
            openTask(taskEl.dataset.task);
            return;
        }
        const histEl = e.target.closest('[data-history]');
        if (histEl) {
            e.preventDefault();
            openHistory(histEl.dataset.history);
            return;
        }
        const diffEl = e.target.closest('[data-diff]');
        if (diffEl) {
            e.preventDefault();
            const [path, from, to] = diffEl.dataset.diff.split('|');
            showDiff(path, Number(from), Number(to));
            return;
        }
        const skillEl = e.target.closest('[data-skill]');
        if (skillEl) {
            e.preventDefault();
            openSkill(skillEl.dataset.skill);
            return;
        }
        const resEl = e.target.closest('[data-resource]');
        if (resEl) {
            e.preventDefault();
            const [name, path] = resEl.dataset.resource.split('|');
            openSkillResource(name, path);
            return;
        }
        const approvalEl = e.target.closest('[data-approve]');
        if (approvalEl) {
            e.preventDefault();
            decideApproval(approvalEl.dataset.approve, true);
            return;
        }
        const rejectEl = e.target.closest('[data-reject]');
        if (rejectEl) {
            e.preventDefault();
            decideApproval(rejectEl.dataset.reject, false);
        }
    });
}

/* ------------------------------------------------------------------ 状态 */
async function loadStatus() {
    const data = await apiGet('/api/harness/status');
    const h = data.harness || {};
    const sandbox = h.sandbox || {};
    const memory = (h.memory && h.memory.long_term) || {};
    const kafka = h.kafka || {};
    const pills = [
        pill('技能', h.skills != null ? h.skills : '-', 'ok'),
        pill('并行子 Agent', h.max_parallel_subagents != null ? h.max_parallel_subagents : '-', ''),
        pill('沙箱', `${sandbox.provider || 'local'}${sandbox.degraded ? ' / 降级' : ''}`, sandbox.degraded ? 'warn' : 'ok'),
        pill('Milvus', memory.milvus_ready ? '已连接' : (memory.milvus_enabled === false ? '未启用' : '本地降级'), memory.milvus_ready ? 'ok' : 'warn'),
        pill('Kafka', kafka.available ? '已连接' : (kafka.enabled === false ? '未启用' : '本地缓冲'), kafka.available ? 'ok' : 'warn'),
    ];
    $('#hx-status').innerHTML = pills.join('');
}

async function loadRuns() {
    const data = await apiGet('/api/harness/runs?limit=100');
    HX.runs = data.runs || [];
    renderRuns();
}

function renderRuns() {
    const box = $('#hx-runs');
    $('#hx-run-count').textContent = HX.runs.length;
    if (!HX.runs.length) {
        box.innerHTML = '<div class="hx-empty">还没有运行记录<br>点击右上角「新建运行」开始</div>';
        return;
    }
    box.innerHTML = HX.runs.map((run) => `
        <div class="hx-run-item ${run.id === HX.runId ? 'active' : ''}" data-run="${esc(run.id)}">
            <div class="hx-run-obj">${esc(run.objective)}</div>
            <div class="hx-run-sub">
                ${stateBadge(run.status)}
                <span class="hx-muted">${esc(fmtDateTime(run.created_at))}</span>
            </div>
        </div>
    `).join('');
    box.querySelectorAll('[data-run]').forEach((el) => {
        el.addEventListener('click', () => selectRun(el.dataset.run));
    });
}

/* ------------------------------------------------------------------ 运行 */
async function createRun() {
    const objective = $('#hx-objective').value.trim();
    const errorEl = $('#hx-new-error');
    errorEl.textContent = '';
    if (objective.length < 4) {
        errorEl.textContent = '请至少输入 4 个字符的目标描述';
        return;
    }
    const btn = $('#hx-create-run');
    btn.disabled = true;
    btn.textContent = '启动中…';
    try {
        const run = await apiPost('/api/harness/runs', { objective });
        $('#hx-modal-new').classList.add('hidden');
        $('#hx-objective').value = '';
        await loadRuns();
        await selectRun(run.id);
        showToast('运行已启动', 'success');
    } catch (err) {
        errorEl.textContent = err.message || '创建失败';
    } finally {
        btn.disabled = false;
        btn.textContent = '启动运行';
    }
}

async function selectRun(runId) {
    if (HX.streamAbort) {
        HX.streamAbort.abort();
        HX.streamAbort = null;
    }
    HX.runId = runId;
    HX.events = [];
    HX.lastSeq = 0;
    renderTimeline();
    renderRuns();
    try {
        const run = await apiGet(`/api/harness/runs/${runId}`);
        HX.run = run;
        renderRun();
        await Promise.all([loadApprovals(), loadFiles('/'), loadTraces()]);
        if (!isTerminal(run.status)) {
            openStream(runId, 0);
        }
    } catch (err) {
        showToast(err.message || '加载运行失败', 'error');
    }
}

async function refreshRun() {
    if (!HX.runId) return;
    try {
        const run = await apiGet(`/api/harness/runs/${HX.runId}`);
        HX.run = run;
        renderRun();
        await Promise.all([loadApprovals(), loadFiles('/'), loadTraces()]);
    } catch (err) {
        console.warn(err);
    }
}

function renderRun() {
    const run = HX.run;
    if (!run) return;
    $('#hx-run-title').textContent = run.objective;
    const counters = run.counters || {};
    const meta = [
        stateBadge(run.status),
        `<span>ID <code>${esc(run.id)}</code></span>`,
        `<span>创建 ${esc(fmtDateTime(run.created_at))}</span>`,
        `<span>任务 ${counters.tasks_completed || 0}/${counters.tasks_total || 0}</span>`,
        `<span>工具调用 ${counters.tool_calls || 0}</span>`,
        `<span>召回经验 ${counters.memories_recalled || 0}</span>`,
        `<span>沉淀经验 ${counters.memories_distilled || 0}</span>`,
    ];
    if (run.error) meta.push(`<span style="color:var(--hx-red)">${esc(run.error)}</span>`);
    $('#hx-run-meta').innerHTML = meta.join('');

    const actions = [];
    if (!isTerminal(run.status)) {
        actions.push('<button id="hx-resume-btn" class="hx-btn ghost">继续 / 重试</button>');
        actions.push('<button id="hx-cancel-btn" class="hx-btn danger">取消运行</button>');
    }
    actions.push('<button id="hx-detail-btn" class="hx-btn ghost">运行详情</button>');
    $('#hx-run-actions').innerHTML = actions.join('');
    const resumeBtn = $('#hx-resume-btn');
    if (resumeBtn) resumeBtn.addEventListener('click', resumeRun);
    const cancelBtn = $('#hx-cancel-btn');
    if (cancelBtn) cancelBtn.addEventListener('click', cancelRun);
    const detailBtn = $('#hx-detail-btn');
    if (detailBtn) detailBtn.addEventListener('click', () => openTask(null));

    renderBoard();
    renderReport();
}

async function resumeRun() {
    if (!HX.runId) return;
    try {
        await apiPost(`/api/harness/runs/${HX.runId}/resume`, { reason: 'manual resume' });
        showToast('已触发继续执行', 'success');
        await refreshRun();
        if (!isTerminal((HX.run || {}).status)) openStream(HX.runId, HX.lastSeq);
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function cancelRun() {
    if (!HX.runId) return;
    if (!window.confirm('确认取消当前运行？')) return;
    try {
        await apiPost(`/api/harness/runs/${HX.runId}/cancel`, {});
        showToast('已取消', 'warning');
        await refreshRun();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

/* ------------------------------------------------------------------ 任务板 */
function renderBoard() {
    const tasks = (HX.run && HX.run.plan && HX.run.plan.tasks) || [];
    const buckets = { pending: [], in_progress: [], completed: [], failed: [] };
    tasks.forEach((task) => {
        if (task.status === 'in_progress' || task.status === 'waiting_approval') {
            buckets.in_progress.push(task);
        } else if (task.status === 'completed' || task.status === 'skipped') {
            buckets.completed.push(task);
        } else if (task.status === 'failed' || task.status === 'blocked') {
            buckets.failed.push(task);
        } else {
            buckets.pending.push(task);
        }
    });
    const render = (list) => list.map(taskCard).join('') || '<div class="hx-empty">—</div>';
    $('#hx-col-pending').innerHTML = render(buckets.pending);
    $('#hx-col-progress').innerHTML = render(buckets.in_progress);
    $('#hx-col-completed').innerHTML = render(buckets.completed);
    $('#hx-col-failed').innerHTML = render(buckets.failed);
    $('#hx-cnt-pending').textContent = buckets.pending.length;
    $('#hx-cnt-progress').textContent = buckets.in_progress.length;
    $('#hx-cnt-completed').textContent = buckets.completed.length;
    $('#hx-cnt-failed').textContent = buckets.failed.length;
}

function taskCard(task) {
    const cls = task.status === 'in_progress' ? 'running' : (task.status === 'failed' || task.status === 'blocked' ? 'failed' : '');
    const artifacts = (task.artifacts || []).map((path) => `<div data-file="${esc(path)}">${esc(path)}</div>`).join('');
    return `
        <div class="hx-task ${cls}" data-task="${esc(task.id)}">
            <div class="hx-task-title">${task.seq}. ${esc(task.title)}</div>
            <div class="hx-task-meta">
                <span class="hx-task-agent">${esc(AGENT_LABEL[task.agent] || task.agent)}</span>
                <span>${esc(statusLabel(task.status))}${task.attempts > 1 ? ` · 第${task.attempts}次` : ''}</span>
            </div>
            ${task.duration_ms != null ? `<div class="hx-task-meta"><span>耗时 ${fmtDuration(task.duration_ms)}</span><span>${task.depends_on && task.depends_on.length ? '有依赖' : '可并行'}</span></div>` : ''}
            ${artifacts ? `<div class="hx-task-artifacts">${artifacts}</div>` : ''}
        </div>
    `;
}

function openTask(taskId) {
    if (!HX.run) return;
    if (!taskId) {
        modal('运行详情', `<pre>${esc(JSON.stringify(HX.run, null, 2))}</pre>`);
        return;
    }
    const tasks = (HX.run.plan && HX.run.plan.tasks) || [];
    const task = tasks.find((item) => item.id === taskId);
    if (!task) return;
    const artifacts = (task.artifacts || []).map((p) => `<div data-file="${esc(p)}" class="hx-file"><span class="hx-file-name">${esc(p)}</span></div>`).join('') || '<span class="hx-muted">无</span>';
    modal(`任务 ${task.seq} · ${task.title}`, `
        <p>${esc(task.description || '')}</p>
        <p class="hx-muted">Agent：${esc(AGENT_LABEL[task.agent] || task.agent)} · 状态：${esc(statusLabel(task.status))} · 尝试：${task.attempts}</p>
        <h4>验收标准</h4>
        <ul>${(task.acceptance || []).map((a) => `<li>${esc(a)}</li>`).join('') || '<li class="hx-muted">无</li>'}</ul>
        <h4>结论</h4>
        <pre>${esc(task.result_summary || task.error || '暂无')}</pre>
        <h4>产出文件</h4>
        <div class="hx-pane-body" style="padding:0">${artifacts}</div>
    `);
}

/* ------------------------------------------------------------------ 时间线 */
function renderTimeline() {
    const list = HX.events.slice(-200);
    $('#hx-event-count').textContent = `${HX.events.length} 事件`;
    if (!list.length) {
        $('#hx-timeline').innerHTML = '<div class="hx-empty">等待事件…</div>';
        return;
    }
    const box = $('#hx-timeline');
    box.innerHTML = list.map((ev) => {
        const cls = eventClass(ev.type);
        return `
            <div class="hx-event ${cls}">
                <time>${esc(fmtTime(ev.ts))}</time>
                <span class="hx-ev-type" title="${esc(ev.type)}">${esc(ev.type)}</span>
                <span class="hx-ev-body">${esc(eventSummary(ev))}</span>
            </div>
        `;
    }).join('');
    box.scrollTop = box.scrollHeight;
}

function eventClass(type) {
    if (type.indexOf('failed') >= 0 || type.indexOf('error') >= 0) return 'error';
    if (type.indexOf('completed') >= 0) return 'done';
    if (type.indexOf('approval') >= 0) return 'approval';
    if (type.indexOf('tool') >= 0) return 'tool';
    return '';
}

function eventSummary(ev) {
    const d = ev.payload || {};
    switch (ev.type) {
        case 'run.created': return '运行已创建';
        case 'run.started': return '开始执行';
        case 'plan.created': return `规划 ${(d.tasks || []).length} 个任务：${d.strategy || ''}`;
        case 'task.started': return `▶ ${d.title || d.task_id}（${AGENT_LABEL[d.agent] || d.agent || ''}）`;
        case 'task.updated': return `状态 → ${statusLabel(d.status)}：${d.title || d.task_id}`;
        case 'task.completed': return `✓ ${d.title || d.task_id}`;
        case 'task.failed': return `✗ ${d.title || d.task_id}：${d.error || ''}`;
        case 'agent.delegated': return `委派 ${AGENT_LABEL[d.agent] || d.agent}`;
        case 'agent.completed': return `子 Agent 完成：${AGENT_LABEL[d.agent] || d.agent} · ${d.success ? '成功' : '失败'}`;
        case 'tool.started': return `调用工具 ${d.tool}`;
        case 'tool.completed': return `${d.success ? '完成' : '失败'} ${d.tool}（${d.duration_ms || 0} ms）`;
        case 'approval.requested': return `待审批：${d.action} → ${d.target || ''}`;
        case 'approval.decided': return `审批 ${d.approval ? statusLabel(d.approval.status) : ''}`;
        case 'run.waiting_approval': return `等待人工审批（${d.action || ''}）`;
        case 'run.completed': return `运行完成：${d.report_path || ''}`;
        case 'run.failed': return `运行失败：${d.error || ''}`;
        case 'run.cancelled': return '运行已取消';
        case 'memory.recalled': return `召回 ${d.count || 0} 条历史经验`;
        case 'memory.distilled': return `沉淀 ${d.count || 0} 条新经验`;
        case 'file.written': return `写入 ${d.path}`;
        case 'heartbeat': return '';
        default: return ev.type;
    }
}

function handleEvent(type, data) {
    if (type === 'heartbeat') return;
    HX.events.push(data);
    if (HX.events.length > 1200) HX.events.splice(0, HX.events.length - 1200);
    HX.lastSeq = Math.max(HX.lastSeq, data.seq || 0);

    if (type === 'plan.created') {
        if (!HX.run) HX.run = {};
        HX.run.plan = {
            strategy: data.strategy || '',
            tasks: (data.tasks || []).map((task) => ({
                ...task,
                status: task.status || 'pending',
                artifacts: task.artifacts || [],
                acceptance: task.acceptance || [],
            })),
        };
    }

    if (type === 'task.started' || type === 'task.updated' || type === 'task.completed' || type === 'task.failed') {
        const task = findTask(data.task_id);
        if (task) {
            if (data.status) task.status = data.status;
            if (data.error != null) task.error = data.error;
            if (data.summary) task.result_summary = data.summary;
            if (data.artifacts) task.artifacts = data.artifacts;
        }
    }

    if (type === 'run.started' && HX.run) HX.run.status = 'running';
    if (type === 'run.waiting_approval' && HX.run) HX.run.status = 'waiting_approval';
    if (type === 'run.completed' || type === 'run.failed' || type === 'run.cancelled') {
        if (HX.run) {
            HX.run.status = type === 'run.completed' ? 'completed' : (type === 'run.failed' ? 'failed' : 'cancelled');
        }
        setTimeout(refreshRun, 250);
    }

    if (type === 'approval.requested') {
        showToast('有新的高风险动作待审批', 'warning');
        activateTab('approvals');
        loadApprovals();
    }
    if (type === 'approval.decided') loadApprovals();
    if (type === 'file.written') loadFiles('/');
    if (type === 'memory.distilled') showToast(`已沉淀 ${data.count || 0} 条经验`, 'success');

    renderTimeline();
    if (type === 'plan.created' || type.startsWith('task.')) renderBoard();
}

function findTask(taskId) {
    if (!HX.run || !HX.run.plan) return null;
    return (HX.run.plan.tasks || []).find((task) => task.id === taskId) || null;
}

/* ------------------------------------------------------------------ SSE */
async function openStream(runId, afterSeq) {
    if (!isAuthenticated()) return;
    const ctrl = new AbortController();
    HX.streamAbort = ctrl;
    try {
        const response = await fetch(`/api/harness/runs/${runId}/events?after_seq=${afterSeq}`, {
            headers: { Authorization: `Bearer ${accessToken}` },
            signal: ctrl.signal,
        });
        if (response.status === 401) {
            clearTokens();
            showLogin();
            return;
        }
        if (!response.ok) throw new Error(`SSE ${response.status}`);
        await consumeSSE(response, (type, data) => {
            if (HX.runId === runId) handleEvent(type, data);
        });
    } catch (err) {
        if (err.name !== 'AbortError') console.warn('stream ended', err);
    }
    if (HX.runId !== runId) return;
    try {
        const detail = await apiGet(`/api/harness/runs/${runId}`);
        HX.run = detail;
        renderRun();
        if (!isTerminal(detail.status)) {
            setTimeout(() => openStream(runId, HX.lastSeq), 1200);
        } else {
            await Promise.all([loadApprovals(), loadFiles('/'), loadTraces()]);
        }
    } catch (err) {
        console.warn(err);
    }
}

/* ------------------------------------------------------------------ 报告 */
async function renderReport() {
    const box = $('#hx-report');
    const openBtn = $('#hx-open-report');
    if (!HX.run) {
        box.className = 'hx-report hx-empty';
        box.textContent = '选择一个运行查看报告';
        openBtn.disabled = true;
        return;
    }
    const reportPath = HX.run.result && HX.run.result.report_path;
    if (!reportPath) {
        box.className = 'hx-report hx-empty';
        box.textContent = isTerminal(HX.run.status) ? '本次运行未生成报告' : '运行完成后在此展示最终分析报告';
        openBtn.disabled = true;
        return;
    }
    openBtn.disabled = false;
    box.className = 'hx-report';
    try {
        const file = await apiGet(`/api/harness/runs/${HX.runId}/file?path=${encodeURIComponent(reportPath)}`);
        box.innerHTML = md(file.content);
    } catch (err) {
        const summary = (HX.run.result && HX.run.result.summary) || '';
        box.innerHTML = summary ? md(summary) : `<div class="hx-empty">报告读取失败：${esc(err.message)}</div>`;
    }
}

/* ------------------------------------------------------------------ 审批 */
async function loadApprovals() {
    try {
        const query = HX.runId ? `?status=pending&run_id=${encodeURIComponent(HX.runId)}` : '?status=pending';
        const data = await apiGet('/api/harness/approvals' + query);
        HX.approvals = data.approvals || [];
        renderApprovals();
    } catch (err) {
        console.warn(err);
    }
}

function renderApprovals() {
    const box = $('#hx-approvals');
    const badge = $('#hx-badge-approvals');
    if (!HX.approvals.length) {
        box.className = 'hx-pane-body hx-empty';
        box.innerHTML = '暂无待审批动作';
        badge.classList.add('hidden');
        return;
    }
    badge.textContent = HX.approvals.length;
    badge.classList.remove('hidden');
    box.className = 'hx-pane-body';
    box.innerHTML = HX.approvals.map((item) => `
        <div class="hx-approval">
            <div class="hx-ap-head">
                <span class="hx-ap-action">${esc(item.action)}</span>
                <span class="hx-risk ${esc(item.risk)}">${esc(item.risk)}</span>
            </div>
            <div class="hx-ap-target">${esc(item.target || '-')}</div>
            <div class="hx-ap-reason">${esc(item.reason || '')}</div>
            <div class="hx-muted">${esc(fmtDateTime(item.created_at))} · 过期 ${esc(fmtDateTime(item.expires_at))}</div>
            <div class="hx-ap-actions">
                <button class="hx-btn primary small" data-approve="${esc(item.id)}">通过</button>
                <button class="hx-btn danger small" data-reject="${esc(item.id)}">拒绝</button>
            </div>
        </div>
    `).join('');
}

async function decideApproval(approvalId, approved) {
    const comment = window.prompt(approved ? '审批意见（可选）' : '拒绝原因（必填）', '') || '';
    if (!approved && !comment.trim()) {
        showToast('拒绝时必须填写原因', 'error');
        return;
    }
    try {
        await apiPost(`/api/harness/approvals/${approvalId}/decision`, { approved, comment });
        showToast(approved ? '已通过审批' : '已拒绝审批', approved ? 'success' : 'warning');
        await loadApprovals();
        if (HX.runId) await refreshRun();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

/* ------------------------------------------------------------------ 文件 */
async function loadFiles(path) {
    if (!HX.runId) return;
    try {
        const data = await apiGet(`/api/harness/runs/${HX.runId}/files?path=${encodeURIComponent(path || '/')}&recursive=true`);
        const files = (data.entries || []).filter((entry) => entry.kind === 'file');
        const groups = {};
        files.forEach((file) => {
            const parts = file.path.split('/').filter(Boolean);
            const group = parts.length > 1 ? parts[0] : 'root';
            (groups[group] = groups[group] || []).push(file);
        });
        const box = $('#hx-files');
        if (!files.length) {
            box.className = 'hx-pane-body hx-empty';
            box.textContent = '暂无产出文件';
            return;
        }
        box.className = 'hx-pane-body';
        box.innerHTML = Object.keys(groups).sort().map((group) => `
            <div class="hx-file-group">
                <div class="hx-file-group-title">/${esc(group)}</div>
                ${groups[group].map((file) => `
                    <div class="hx-file" data-file="${esc(file.path)}">
                        <span class="hx-file-name">${esc(file.path.split('/').pop())}</span>
                        <span class="hx-file-size">v${file.version} · ${formatBytes(file.size || 0)}</span>
                    </div>
                `).join('')}
            </div>
        `).join('');
    } catch (err) {
        console.warn(err);
    }
}

async function openFile(path) {
    if (!HX.runId) return;
    try {
        const data = await apiGet(`/api/harness/runs/${HX.runId}/file?path=${encodeURIComponent(path)}`);
        const entry = data.entry || {};
        const isMarkdown = /\.(md|markdown)$/i.test(path);
        const body = isMarkdown ? md(data.content) : `<pre>${esc(data.content)}</pre>`;
        modal(path, `
            <p class="hx-muted">版本 v${entry.version || 0} · ${formatBytes(entry.size || 0)} · 作者 ${esc(entry.author || '-')}</p>
            <div class="hx-modal-actions" style="justify-content:flex-start">
                <button class="hx-btn ghost small" data-history="${esc(path)}">版本历史</button>
            </div>
            <div class="hx-report" style="max-height:none;padding:0;margin-top:10px">${body}</div>
        `);
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function openHistory(path) {
    if (!HX.runId) return;
    try {
        const data = await apiGet(`/api/harness/runs/${HX.runId}/file/history?path=${encodeURIComponent(path)}`);
        const versions = data.versions || [];
        const rows = versions.map((v, index) => `
            <tr>
                <td>v${v.version}</td>
                <td>${esc(fmtDateTime(v.created_at))}</td>
                <td>${esc(v.author || '-')}</td>
                <td>${esc(v.comment || '')}</td>
                <td>${formatBytes(v.size || 0)}</td>
                <td>${index > 0 ? `<button class="hx-btn ghost small" data-diff="${esc(path)}|${versions[index - 1].version}|${v.version}">对比上一版</button>` : ''}</td>
            </tr>
        `).join('');
        modal(`版本历史 · ${path}`, `
            <table class="hx-table">
                <thead><tr><th>版本</th><th>时间</th><th>作者</th><th>说明</th><th>大小</th><th></th></tr></thead>
                <tbody>${rows || '<tr><td colspan="6" class="hx-muted">暂无版本</td></tr>'}</tbody>
            </table>
        `);
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function showDiff(path, from, to) {
    if (!HX.runId) return;
    try {
        const data = await apiGet(`/api/harness/runs/${HX.runId}/file/diff?path=${encodeURIComponent(path)}&from_version=${from}&to_version=${to}`);
        modal(`版本对比 · ${path}`, `<pre>${esc(data.diff || '（无差异）')}</pre>`);
    } catch (err) {
        showToast(err.message, 'error');
    }
}

/* ------------------------------------------------------------------ 技能 */
async function loadSkills() {
    try {
        const data = await apiGet('/api/harness/skills');
        HX.skills = data.skills || [];
        const box = $('#hx-skills');
        if (!HX.skills.length) {
            box.className = 'hx-pane-body hx-empty';
            box.textContent = '未加载到技能';
            return;
        }
        box.className = 'hx-pane-body';
        box.innerHTML = HX.skills.map((skill) => `
            <div class="hx-skill" data-skill="${esc(skill.name)}">
                <div class="hx-skill-name">${esc(skill.name)} <span class="hx-muted">v${esc(skill.version)}</span></div>
                <div class="hx-skill-desc">${esc(skill.description)}</div>
                <div class="hx-skill-tags">
                    ${(skill.triggers || []).slice(0, 4).map((t) => `<span class="hx-tag">${esc(t)}</span>`).join('')}
                </div>
            </div>
        `).join('');
    } catch (err) {
        console.warn(err);
    }
}

async function openSkill(name) {
    try {
        const data = await apiGet(`/api/harness/skills/${encodeURIComponent(name)}`);
        const meta = data.meta || {};
        const resources = (meta.resources || []).map((path) => `
            <div class="hx-file" data-resource="${esc(name)}|${esc(path)}">
                <span class="hx-file-name">${esc(path)}</span>
            </div>
        `).join('');
        modal(`技能 · ${name}`, `
            <p class="hx-muted">${esc(meta.description || '')}</p>
            ${resources ? `<h4>资源文件</h4><div class="hx-pane-body" style="padding:0">${resources}</div>` : ''}
            <div class="hx-report" style="max-height:none;padding:0;margin-top:10px">${md(data.body || '')}</div>
        `);
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function openSkillResource(name, path) {
    try {
        const data = await apiGet(`/api/harness/skills/${encodeURIComponent(name)}/resource?path=${encodeURIComponent(path)}`);
        modal(`${name} · ${path}`, `<pre>${esc(data.content || '')}</pre>`);
    } catch (err) {
        showToast(err.message, 'error');
    }
}

/* ------------------------------------------------------------------ 追踪 */
async function loadTraces() {
    if (!HX.runId) return;
    try {
        const data = await apiGet(`/api/harness/runs/${HX.runId}/traces`);
        const spans = data.spans || [];
        const box = $('#hx-traces');
        if (!spans.length) {
            box.className = 'hx-pane-body hx-empty';
            box.textContent = '暂无链路数据';
            return;
        }
        box.className = 'hx-pane-body';
        box.innerHTML = spans.slice(-80).reverse().map((span) => {
            const duration = span.start_ts && span.end_ts
                ? Math.max(0, new Date(span.end_ts) - new Date(span.start_ts))
                : 0;
            return `
                <div class="hx-trace ${esc(span.status || 'ok')}">
                    <div>
                        <div class="hx-trace-name">${esc(span.name)}</div>
                        <div class="hx-trace-kind">${esc(span.kind)} · ${esc(span.status)}</div>
                    </div>
                    <div class="hx-trace-ms">${fmtDuration(duration)}</div>
                </div>
            `;
        }).join('');
    } catch (err) {
        console.warn(err);
    }
}

/* ------------------------------------------------------------------ 记忆 */
async function searchMemory() {
    const q = $('#hx-memory-q').value.trim();
    if (!q) {
        showToast('请输入检索关键字', 'warning');
        return;
    }
    try {
        const data = await apiGet(`/api/harness/memory/search?q=${encodeURIComponent(q)}&top_k=8`);
        renderMemories(data.memories || []);
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function renderMemories(records) {
    const box = $('#hx-memory');
    if (!records.length) {
        box.className = 'hx-empty';
        box.textContent = '没有匹配的长期记忆';
        return;
    }
    box.className = '';
    box.innerHTML = records.map((record) => `
        <div class="hx-memory-item">
            <div class="hx-memory-kind">${esc(record.kind)} · score ${Number(record.score || 0).toFixed(3)}</div>
            <div class="hx-memory-text">${esc(record.text)}</div>
            <div class="hx-memory-meta">${(record.tags || []).map((t) => `#${esc(t)}`).join(' ')}</div>
        </div>
    `).join('');
}

/* ------------------------------------------------------------------ 通用 */
function activateTab(name) {
    HX.activeTab = name;
    $$('.hx-tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === name));
    $$('.hx-pane').forEach((pane) => pane.classList.toggle('active', pane.dataset.pane === name));
    if (name === 'files' && HX.runId) loadFiles('/');
    if (name === 'traces' && HX.runId) loadTraces();
    if (name === 'approvals') loadApprovals();
}

function modal(title, html) {
    $('#hx-modal-title').textContent = title;
    $('#hx-modal-body').innerHTML = html;
    $('#hx-modal').classList.remove('hidden');
}

/* 供任务板点击运行详情时复用 */
window.hxOpenTask = openTask;
