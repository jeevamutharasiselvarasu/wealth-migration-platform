export function renderLogin(container, onSuccess) {
  container.innerHTML = `
    <div class="login-modal">
      <div class="login-card">
        <div class="login-brand">
          <div class="mono-logo">XA</div>
          <div><div class="ttl">XODUS<b>-AMP</b></div><div class="sub">Migration Control Plane</div></div>
        </div>
        <h2>Sign in to continue</h2>
        <form id="loginForm">
          <div class="field">
            <label>Username</label>
            <input type="text" id="usernameInput" placeholder="e.g. Priya or Marcus" required autofocus />
          </div>
          <div class="field">
            <label>Password</label>
            <input type="password" value="demo123" required />
          </div>
          <button type="submit" class="signin-btn">Sign In →</button>
        </form>
      </div>
    </div>
  `;

  document.getElementById('loginForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const username = document.getElementById('usernameInput').value.trim();
    if (username) onSuccess(username);
  });
}