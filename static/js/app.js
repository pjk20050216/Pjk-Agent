// Application initialization and orchestration
document.addEventListener('DOMContentLoaded', () => {
    initAuth();
    initChatInput();
    initUploadModal();
    initSuggestionChips();

    // On auth success
    window.addEventListener('auth-success', async (e) => {
        const user = e.detail;
        $('#sidebar-username').textContent = user.username;
        await loadConversations();
        showWelcomeScreen();
    });

    // On auth expired
    window.addEventListener('auth-expired', () => {
        location.reload();
    });
});
