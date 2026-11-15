let setup = false;
let register = false;   // 已有主账户时，登录页可切换到「注册新账户」
let configured = false;
let ready = false;
const form = document.querySelector('#loginForm');
const enterButton = document.querySelector('#enterButton');
const retryButton = document.querySelector('#retryConnection');
async function initializeLogin() {
  ready = false;
  enterButton.disabled = true;
  retryButton.hidden = true;
  document.querySelector('#formError').textContent = '';
  try {
    const response = await fetch('/api/auth/status');
    if (!response.ok) throw new Error('无法读取账户状态');
    const status = await response.json();
    if (typeof status.configured !== 'boolean') throw new Error('账户状态无效');
    if (status.user) { location.reload(); return; }
    configured = status.configured;
    setup = !status.configured;
    applyMode();
  } catch (_) {
    document.querySelector('#formError').textContent = '无法连接工作台，请确认服务已启动后重试。';
    retryButton.hidden = false;
  }
}
function applyMode() {
    const creating = setup || register;
    const toggle = document.querySelector('#toggleRegister');
    toggle.hidden = setup;
    toggle.textContent = register ? '已有账户？返回登录' : '没有账户？注册一个';
    document.querySelector('#formTitle').textContent = setup ? '建立你的创作空间' : register ? '注册新账户' : '进入创作工作台';
    document.querySelector('#formSubtitle').textContent = setup ? '首次使用，为这台机器创建一个账户。' : register ? '在这台机器上再添加一个账户。' : '继续你与 Agent 的下一次创造。';
    document.querySelector('#confirmRow').hidden = !creating;
    document.querySelector('#confirmPassword').required = creating;
    document.querySelector('#password').minLength = creating ? 10 : 1;
    document.querySelector('#password').autocomplete = creating ? 'new-password' : 'current-password';
    document.querySelector('#password').placeholder = creating ? '设置密码，至少 10 个字符' : '输入密码';
    enterButton.innerHTML = (setup ? '创建账户并进入' : register ? '注册并进入' : '进入工作台') + ' <span>↗</span>';
    document.querySelector('#accountHint').textContent = creating ? '账户保存在本机。密码以加盐哈希存储，无需云端注册。' : '使用本机账户登录，连接你的项目与风格 Agent。';
    document.querySelector('#formError').textContent = '';
    ready = true;
    enterButton.disabled = false;
}
document.querySelector('#toggleRegister').onclick = () => { if (!configured) return; register = !register; applyMode(); };
retryButton.onclick = initializeLogin;
initializeLogin();
document.querySelector('#showPassword').onclick = e => {
  const input = document.querySelector('#password');
  input.type = input.type === 'password' ? 'text' : 'password';
  e.target.textContent = input.type === 'password' ? '显示' : '隐藏';
  e.target.setAttribute('aria-label', input.type === 'password' ? '显示密码' : '隐藏密码');
  e.target.setAttribute('aria-pressed', String(input.type === 'text'));
};
form.onsubmit = async e => {
  e.preventDefault();
  if (!ready || enterButton.disabled) return;
  const error = document.querySelector('#formError');
  const button = document.querySelector('#enterButton');
  error.textContent = '';
  if ((setup || register) && form.password.value !== document.querySelector('#confirmPassword').value) {
    error.textContent = '两次输入的密码不一致。'; return;
  }
  button.disabled = true;
  try {
    const response = await fetch(`/api/auth/${setup ? 'setup' : register ? 'register' : 'login'}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: form.username.value.trim(), password: form.password.value})
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || '登录失败，请重试。');
    location.assign('/');
  } catch (e) { error.textContent = e.message; }
  finally { button.disabled = false; }
};
