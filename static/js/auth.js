// Authentication UI logic
function initAuth() {
    const authContainer = $('#auth-container');
    const appContainer = $('#app-container');
    const loginForm = $('#login-form');
    const registerForm = $('#register-form');
    const tabs = $$('.auth-tab');

    // Tab switching
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            tabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            const target = tab.dataset.tab;
            if (target === 'login') {
                loginForm.classList.remove('hidden');
                registerForm.classList.add('hidden');
            } else {
                registerForm.classList.remove('hidden');
                loginForm.classList.add('hidden');
            }
        });
    });

    // Login
    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const username = $('#login-username').value.trim();
        const password = $('#login-password').value;
        const errorEl = $('#login-error');

        if (!username || !password) {
            errorEl.textContent = 'Please fill in all fields';
            return;
        }

        try {
            errorEl.textContent = '';
            const data = await apiPost('/api/auth/login', { username, password });
            setTokens(data.access_token, data.refresh_token);
            authContainer.classList.add('hidden');
            appContainer.classList.remove('hidden');
            window.dispatchEvent(new CustomEvent('auth-success', { detail: data.user }));
        } catch (err) {
            errorEl.textContent = err.message;
        }
    });

    // Register
    registerForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const username = $('#register-username').value.trim();
        const email = $('#register-email').value.trim();
        const password = $('#register-password').value;
        const errorEl = $('#register-error');

        if (!username || !email || !password) {
            errorEl.textContent = 'Please fill in all fields';
            return;
        }
        if (password.length < 6) {
            errorEl.textContent = 'Password must be at least 6 characters';
            return;
        }

        try {
            errorEl.textContent = '';
            const data = await apiPost('/api/auth/register', { username, email, password });
            setTokens(data.access_token, data.refresh_token);
            authContainer.classList.add('hidden');
            appContainer.classList.remove('hidden');
            window.dispatchEvent(new CustomEvent('auth-success', { detail: data.user }));
        } catch (err) {
            errorEl.textContent = err.message;
        }
    });

    // Check existing auth
    if (isAuthenticated()) {
        apiGet('/api/auth/me').then(user => {
            authContainer.classList.add('hidden');
            appContainer.classList.remove('hidden');
            window.dispatchEvent(new CustomEvent('auth-success', { detail: user }));
        }).catch(() => {
            clearTokens();
        });
    }
}
