// PanGDrive Sync Web UI Frontend Logic

const state = {
  status: { baidu: {}, gdrive: {} },
  baidu: {
    path: "/",
    items: [],
    selected: new Set(),
    filter: "",
    loading: false,
  },
  gdrive: {
    path: "/",
    items: [],
    selected: new Set(),
    filter: "",
    loading: false,
  },
  tasks: [],
  sseConnected: false,
};

// ==========================================
// Notifications / Toast
// ==========================================
function showToast(msg, type = "info") {
  const container = document.getElementById("toastContainer");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${msg}</span>`;
  container.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateX(100%)";
    setTimeout(() => el.remove(), 250);
  }, 3500);
}

// ==========================================
// API Helpers
// ==========================================
async function fetchAPI(url, options = {}) {
  try {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const data = await res.json();
    return data;
  } catch (err) {
    showToast(`网络请求失败: ${err.message}`, "error");
    throw err;
  }
}

// ==========================================
// Status & Quota
// ==========================================
async function loadStatus() {
  try {
    const res = await fetchAPI("/api/status");
    state.status = res;

    // Baidu indicator
    const bDot = document.getElementById("baiduStatusDot");
    const bText = document.getElementById("baiduStatusText");
    if (res.baidu && res.baidu.authenticated) {
      bDot.className = "dot connected";
      bText.innerText = `百度网盘: ${res.baidu.username} (${res.baidu.vip_name})`;
    } else {
      bDot.className = "dot";
      bText.innerText = "百度网盘: 未连接";
    }

    // GDrive indicator
    const gDot = document.getElementById("gdriveStatusDot");
    const gText = document.getElementById("gdriveStatusText");
    if (res.gdrive && res.gdrive.authenticated) {
      gDot.className = "dot connected";
      gText.innerText = `Google Drive: ${res.gdrive.email}`;
    } else {
      gDot.className = "dot";
      gText.innerText = "Google Drive: 未连接";
    }

    renderQuotaModal();
  } catch (err) {
    console.error("Failed to load status:", err);
  }
}

// ==========================================
// File Explorer
// ==========================================
async function loadFiles(drive) {
  const dState = state[drive];
  dState.loading = true;
  dState.selected.clear();
  renderPaneHeader(drive);
  renderFileList(drive);

  try {
    const res = await fetchAPI(`/api/files?drive=${drive}&path=${encodeURIComponent(dState.path)}`);
    if (res.ok) {
      dState.items = res.items || [];
    } else {
      showToast(`${drive.toUpperCase()} 载入失败: ${res.error}`, "error");
      dState.items = [];
    }
  } catch (err) {
    dState.items = [];
  } finally {
    dState.loading = false;
    renderBreadcrumbs(drive);
    renderFileList(drive);
  }
}

function renderBreadcrumbs(drive) {
  const dState = state[drive];
  const container = document.getElementById(`${drive}Breadcrumbs`);
  if (!container) return;

  container.innerHTML = "";
  const parts = dState.path.split("/").filter(Boolean);

  // Root icon/item
  const rootItem = document.createElement("span");
  rootItem.className = "crumb-item";
  rootItem.innerText = "根目录 /";
  rootItem.onclick = () => navigateTo(drive, "/");
  container.appendChild(rootItem);

  let currentAccum = "";
  parts.forEach((p, idx) => {
    currentAccum += "/" + p;
    const sep = document.createElement("span");
    sep.className = "crumb-separator";
    sep.innerText = "›";
    container.appendChild(sep);

    const crumb = document.createElement("span");
    crumb.className = "crumb-item";
    crumb.innerText = p;
    const target = currentAccum;
    crumb.onclick = () => navigateTo(drive, target);
    container.appendChild(crumb);
  });
}

function navigateTo(drive, path) {
  state[drive].path = path;
  loadFiles(drive);
}

function navigateUp(drive) {
  const curr = state[drive].path;
  if (curr === "/" || !curr) return;
  const parts = curr.split("/").filter(Boolean);
  parts.pop();
  const parent = "/" + parts.join("/");
  navigateTo(drive, parent || "/");
}

function renderPaneHeader(drive) {
  const dState = state[drive];
  const countEl = document.getElementById(`${drive}SelectedCount`);
  if (countEl) {
    countEl.innerText = dState.selected.size > 0 ? `(已选 ${dState.selected.size} 项)` : "";
  }
}

function renderFileList(drive) {
  const dState = state[drive];
  const container = document.getElementById(`${drive}FileList`);
  if (!container) return;

  if (dState.loading) {
    container.innerHTML = `
      <div class="empty-state">
        <svg class="animate-spin" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"></path></svg>
        <span>正在读取目录内容...</span>
      </div>`;
    return;
  }

  const filterText = dState.filter.toLowerCase().trim();
  const filtered = dState.items.filter(it => !filterText || it.name.toLowerCase().includes(filterText));

  if (filtered.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <span>此目录下无文件或未匹配到筛选结果</span>
      </div>`;
    return;
  }

  let html = `
    <table class="file-table">
      <thead>
        <tr>
          <th style="width: 38px;">
            <input type="checkbox" id="${drive}SelectAll" onchange="toggleSelectAll('${drive}', this.checked)" />
          </th>
          <th>文件名</th>
          <th style="width: 100px;">大小</th>
          <th style="width: 150px;">修改时间</th>
          <th style="width: 80px; text-align: right;">操作</th>
        </tr>
      </thead>
      <tbody>
  `;

  filtered.forEach(item => {
    const isSelected = dState.selected.has(item.path);
    const icon = item.isdir
      ? `<svg class="file-icon icon-folder" viewBox="0 0 24 24" fill="currentColor"><path d="M19.5 21a3 3 0 0 0 3-3v-4.5a3 3 0 0 0-3-3h-1.5V9a3 3 0 0 0-3-3h-4.8l-1.6-1.6A3 3 0 0 0 7.5 3.5H4.5A3 3 0 0 0 1.5 6.5v11.5a3 3 0 0 0 3 3h15z"/></svg>`
      : `<svg class="file-icon icon-file" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/></svg>`;

    const clickAction = item.isdir
      ? `onclick="navigateTo('${drive}', '${escapeHtml(item.path)}')"`
      : "";

    html += `
      <tr class="${isSelected ? "selected" : ""}">
        <td>
          <input type="checkbox" ${isSelected ? "checked" : ""} onchange="toggleSelectItem('${drive}', '${escapeHtml(item.path)}', this.checked)" />
        </td>
        <td>
          <div class="file-name-cell" ${clickAction}>
            ${icon}
            <span title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
          </div>
        </td>
        <td>${item.size_str}</td>
        <td>${item.mtime_str}</td>
        <td style="text-align: right;">
          <button class="btn btn-secondary btn-icon" title="传输此项" onclick="openTransferSingle('${drive}', '${escapeHtml(item.path)}', ${item.isdir})">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
          </button>
        </td>
      </tr>
    `;
  });

  html += `</tbody></table>`;
  container.innerHTML = html;
}

function toggleSelectItem(drive, path, checked) {
  if (checked) {
    state[drive].selected.add(path);
  } else {
    state[drive].selected.delete(path);
  }
  renderPaneHeader(drive);
  renderFileList(drive);
}

function toggleSelectAll(drive, checked) {
  const dState = state[drive];
  dState.selected.clear();
  if (checked) {
    const filterText = dState.filter.toLowerCase().trim();
    dState.items
      .filter(it => !filterText || it.name.toLowerCase().includes(filterText))
      .forEach(it => dState.selected.add(it.path));
  }
  renderPaneHeader(drive);
  renderFileList(drive);
}

function onSearchInput(drive, val) {
  state[drive].filter = val;
  renderFileList(drive);
}

function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/'/g, "\\'").replace(/"/g, "&quot;");
}

// ==========================================
// Transfer Bridge & Modals
// ==========================================
function openTransferPrompt(srcDrive, dstDrive, mode = "copy") {
  const srcState = state[srcDrive];
  const dstState = state[dstDrive];

  const modal = document.getElementById("transferModal");
  const modalTitle = document.getElementById("transferModalTitle");
  const srcInput = document.getElementById("transferSource");
  const dstInput = document.getElementById("transferDest");
  const modeSelect = document.getElementById("transferMode");

  modalTitle.innerText = mode === "sync"
    ? `整目录跨云同步: ${srcDrive.toUpperCase()} ➔ ${dstDrive.toUpperCase()}`
    : `跨云文件传输: ${srcDrive.toUpperCase()} ➔ ${dstDrive.toUpperCase()}`;

  let sourceUri = "";
  if (srcState.selected.size > 0) {
    // Pick the first selected or current
    const selectedPaths = Array.from(srcState.selected);
    sourceUri = `${srcDrive}:${selectedPaths[0]}`;
  } else {
    sourceUri = `${srcDrive}:${srcState.path}`;
  }

  const destUri = `${dstDrive}:${dstState.path}`;

  srcInput.value = sourceUri;
  dstInput.value = destUri;
  modeSelect.value = mode;

  modal.classList.add("open");
}

function openTransferSingle(srcDrive, path, isDir) {
  const dstDrive = srcDrive === "baidu" ? "gdrive" : "baidu";
  const dstPath = state[dstDrive].path;
  const mode = isDir ? "sync" : "copy";

  const modal = document.getElementById("transferModal");
  const modalTitle = document.getElementById("transferModalTitle");
  const srcInput = document.getElementById("transferSource");
  const dstInput = document.getElementById("transferDest");
  const modeSelect = document.getElementById("transferMode");

  modalTitle.innerText = `${isDir ? "同步目录" : "传输文件"}: ${srcDrive.toUpperCase()} ➔ ${dstDrive.toUpperCase()}`;
  srcInput.value = `${srcDrive}:${path}`;
  dstInput.value = `${dstDrive}:${dstPath}`;
  modeSelect.value = mode;

  modal.classList.add("open");
}

async function confirmTransfer() {
  const source = document.getElementById("transferSource").value.trim();
  const dest = document.getElementById("transferDest").value.trim();
  const mode = document.getElementById("transferMode").value;
  const skipExisting = document.getElementById("transferSkipExisting").checked;
  const saveAsJob = document.getElementById("transferSaveAsJob")?.checked;
  const jobName = document.getElementById("transferJobName")?.value.trim() || `Sync: ${source} ➔ ${dest}`;

  if (!source || !dest) {
    showToast("源路径和目标路径均不可为空", "error");
    return;
  }

  // If saveAsJob is checked, save persistent sync job rule
  if (saveAsJob) {
    try {
      await fetchAPI("/api/jobs", {
        method: "POST",
        body: JSON.stringify({
          name: jobName,
          source,
          dest,
          mode,
          skip_existing: skipExisting,
          recursive: true,
          interval_seconds: 0,
        }),
      });
      showToast(`已成功保存持久化规则: ${jobName}`, "info");
    } catch (err) {
      console.error("Save job error:", err);
    }
  }

  try {
    const res = await fetchAPI("/api/transfer/start", {
      method: "POST",
      body: JSON.stringify({
        source,
        dest,
        mode,
        skip_existing: skipExisting,
        recursive: true,
      }),
    });

    if (res.ok) {
      showToast(`任务已启动: ${mode.toUpperCase()} ${source} ➔ ${dest}`, "success");
      closeModal("transferModal");
      // Expand task drawer
      document.getElementById("taskDrawer").classList.remove("collapsed");
    } else {
      showToast(`启动任务失败: ${res.error}`, "error");
    }
  } catch (err) {
    showToast(`启动任务异常: ${err.message}`, "error");
  }
}

// ==========================================
// Task Manager & SSE Stream
// ==========================================
function initTaskStream() {
  const eventSource = new EventSource("/api/tasks/events");

  eventSource.onmessage = (event) => {
    try {
      const tasks = JSON.parse(event.data);
      state.tasks = tasks;
      renderTasks();
    } catch (err) {
      console.error("SSE parse error:", err);
    }
  };

  eventSource.onerror = () => {
    // Fallback polling if SSE drops
    if (!state.sseConnected) {
      setInterval(pollTasks, 3000);
      state.sseConnected = true;
    }
  };
}

async function pollTasks() {
  try {
    const res = await fetchAPI("/api/tasks");
    if (res.ok) {
      state.tasks = res.tasks;
      renderTasks();
    }
  } catch (err) {}
}

function renderTasks() {
  const container = document.getElementById("taskListContainer");
  const countBadge = document.getElementById("activeTasksCount");
  if (!container) return;

  const runningCount = state.tasks.filter(t => t.status === "running" || t.status === "pending").length;
  countBadge.innerText = runningCount > 0 ? runningCount : "";
  countBadge.style.display = runningCount > 0 ? "inline-block" : "none";

  if (state.tasks.length === 0) {
    container.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 1.5rem 0; font-size: 0.85rem;">暂无跨云传输任务</div>`;
    return;
  }

  let html = "";
  state.tasks.forEach(t => {
    let statusClass = "tag-copy";
    let statusLabel = t.status.toUpperCase();
    if (t.status === "completed") {
      statusClass = "badge-gdrive";
      statusLabel = "已完成";
    } else if (t.status === "failed") {
      statusClass = "btn-danger";
      statusLabel = "失败";
    } else if (t.status === "cancelled") {
      statusClass = "tag-mode";
      statusLabel = "已取消";
    } else if (t.status === "running") {
      statusClass = "badge-baidu";
      statusLabel = "传输中";
    }

    const cancelBtn = (t.status === "running" || t.status === "pending")
      ? `<button class="btn btn-danger btn-icon" style="padding: 0.2rem 0.5rem; font-size: 0.75rem;" onclick="cancelTask('${t.id}')">取消</button>`
      : "";

    html += `
      <div class="task-card">
        <div class="task-top">
          <div class="task-title">
            <span class="tag-mode ${t.mode === 'sync' ? 'tag-sync' : 'tag-copy'}">${t.mode}</span>
            <span>${escapeHtml(t.source)} ➔ ${escapeHtml(t.dest)}</span>
          </div>
          <div style="display: flex; align-items: center; gap: 0.5rem;">
            <span class="tag-mode ${statusClass}">${statusLabel}</span>
            ${cancelBtn}
          </div>
        </div>
        <div class="progress-bar-bg">
          <div class="progress-bar-fill ${t.status}" style="width: ${t.percent}%;"></div>
        </div>
        <div class="task-bottom">
          <span>${t.current_file ? '正在处理: ' + escapeHtml(t.current_file) : ''} (${t.transferred_bytes_str} / ${t.total_bytes_str})</span>
          <span>${t.percent}% • ${t.speed_str} ${t.eta_seconds ? '• 剩余约 ' + t.eta_seconds + 's' : ''}</span>
        </div>
        ${t.error ? `<div style="font-size: 0.75rem; color: var(--accent-red); margin-top: 0.2rem;">错误: ${escapeHtml(t.error)}</div>` : ''}
      </div>
    `;
  });

  container.innerHTML = html;
}

async function cancelTask(taskId) {
  try {
    await fetchAPI(`/api/tasks/${taskId}/cancel`, { method: "POST" });
    showToast("已请求取消任务", "info");
  } catch (err) {
    showToast(`取消任务失败: ${err.message}`, "error");
  }
}

async function clearCompletedTasks() {
  try {
    await fetchAPI("/api/tasks/clear", { method: "POST" });
    showToast("已清空已完成记录", "info");
  } catch (err) {}
}

function toggleTaskDrawer() {
  const drawer = document.getElementById("taskDrawer");
  drawer.classList.toggle("collapsed");
}

// ==========================================
// Mkdir Modal
// ==========================================
let activeMkdirDrive = "baidu";

function openMkdirModal(drive) {
  activeMkdirDrive = drive;
  document.getElementById("mkdirDriveName").innerText = drive.toUpperCase();
  document.getElementById("mkdirParentPath").innerText = state[drive].path;
  document.getElementById("mkdirFolderName").value = "";
  document.getElementById("mkdirModal").classList.add("open");
}

async function confirmMkdir() {
  const name = document.getElementById("mkdirFolderName").value.trim();
  if (!name) {
    showToast("文件夹名称不能为空", "error");
    return;
  }
  const curr = state[activeMkdirDrive].path;
  const target = `${curr}/${name}`.replace("//", "/");

  try {
    const res = await fetchAPI("/api/files/mkdir", {
      method: "POST",
      body: JSON.stringify({ drive: activeMkdirDrive, path: target }),
    });
    if (res.ok) {
      showToast(`目录已创建: ${target}`, "success");
      closeModal("mkdirModal");
      loadFiles(activeMkdirDrive);
    } else {
      showToast(`创建失败: ${res.error}`, "error");
    }
  } catch (err) {
    showToast(`创建目录异常: ${err.message}`, "error");
  }
}

// ==========================================
// Settings / Auth Modal
// ==========================================
function switchAuthTab(tab) {
  document.querySelectorAll(".auth-tab-btn").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".auth-tab-content").forEach(c => c.style.display = "none");

  document.getElementById(`tabBtn_${tab}`).classList.add("active");
  document.getElementById(`tabContent_${tab}`).style.display = "flex";
}

async function saveBaiduAuth() {
  const bduss = document.getElementById("baiduBdussInput").value.trim();
  const cookies = document.getElementById("baiduCookiesInput").value.trim();

  if (!bduss && !cookies) {
    showToast("请输入 BDUSS 或完整 Cookie 字符串", "error");
    return;
  }

  try {
    const res = await fetchAPI("/api/auth/baidu", {
      method: "POST",
      body: JSON.stringify({ bduss, cookies }),
    });
    if (res.ok) {
      showToast(`百度网盘登录成功: 用户名 ${res.user}`, "success");
      loadStatus();
      loadFiles("baidu");
      closeModal("settingsModal");
    } else {
      showToast(`登录失败: ${res.error}`, "error");
    }
  } catch (err) {
    showToast(`认证请求异常: ${err.message}`, "error");
  }
}

async function saveGDriveAuth() {
  const authType = document.querySelector('input[name="gdriveAuthType"]:checked').value;
  const jsonContent = document.getElementById("gdriveJsonInput").value.trim();
  const token = document.getElementById("gdriveTokenInput").value.trim();

  const payload = { auth_type: authType };
  if (authType === "service_account") {
    if (!jsonContent) {
      showToast("请粘贴或上传 Service Account JSON 密钥", "error");
      return;
    }
    payload.service_account_json = jsonContent;
  } else {
    if (!token) {
      showToast("请输入 Access Token", "error");
      return;
    }
    payload.token = token;
  }

  try {
    const res = await fetchAPI("/api/auth/gdrive", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      showToast(`Google Drive 登录成功: ${res.email}`, "success");
      loadStatus();
      loadFiles("gdrive");
      closeModal("settingsModal");
    } else {
      showToast(`登录失败: ${res.error}`, "error");
    }
  } catch (err) {
    showToast(`认证请求异常: ${err.message}`, "error");
  }
}

function handleGDriveJsonFile(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    document.getElementById("gdriveJsonInput").value = e.target.result;
    showToast(`已加载密钥文件: ${file.name}`, "info");
  };
  reader.readAsText(file);
}

// ==========================================
// Quota Modal
// ==========================================
function renderQuotaModal() {
  const container = document.getElementById("quotaModalBody");
  if (!container) return;

  const bQuota = state.status.baidu?.quota;
  const gQuota = state.status.gdrive?.quota;

  let html = `
    <div style="display: flex; flex-direction: column; gap: 1.25rem;">
      <!-- Baidu Quota -->
      <div style="background: rgba(15, 23, 42, 0.5); padding: 1rem; border-radius: var(--radius-md); border: 1px solid var(--border-color);">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
          <span style="font-weight: 600; color: #38bdf8;">百度网盘存储配额</span>
          <span style="font-size: 0.85rem; color: var(--text-secondary);">${state.status.baidu?.vip_name || '普通用户'}</span>
        </div>
        ${bQuota ? `
          <div class="progress-bar-bg" style="height: 10px; margin-bottom: 0.5rem;">
            <div class="progress-bar-fill" style="width: ${bQuota.percent}%;"></div>
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-secondary);">
            <span>已用: ${bQuota.used_str} (${bQuota.percent}%)</span>
            <span>总计: ${bQuota.total_str}</span>
          </div>
        ` : `<div style="font-size: 0.85rem; color: var(--text-muted);">尚未获取到配额信息</div>`}
      </div>

      <!-- GDrive Quota -->
      <div style="background: rgba(15, 23, 42, 0.5); padding: 1rem; border-radius: var(--radius-md); border: 1px solid var(--border-color);">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
          <span style="font-weight: 600; color: #34d399;">Google Drive 存储配额</span>
          <span style="font-size: 0.85rem; color: var(--text-secondary);">${state.status.gdrive?.email || '-'}</span>
        </div>
        ${gQuota ? `
          <div class="progress-bar-bg" style="height: 10px; margin-bottom: 0.5rem;">
            <div class="progress-bar-fill" style="width: ${gQuota.percent}%; background: var(--accent-green);"></div>
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-secondary);">
            <span>已用: ${gQuota.used_str} (${gQuota.percent}%)</span>
            <span>总计: ${gQuota.total_str}</span>
          </div>
        ` : `<div style="font-size: 0.85rem; color: var(--text-muted);">尚未获取到配额信息</div>`}
      </div>
    </div>
  `;

  container.innerHTML = html;
}

// ==========================================
// Persistent Sync Jobs Management
// ==========================================
async function loadJobs() {
  try {
    const res = await fetchAPI("/api/jobs");
    if (res.ok) {
      state.jobs = res.jobs || [];
      renderJobsModal();
    }
  } catch (err) {
    console.error("Load jobs error:", err);
  }
}

function openJobsModal() {
  openModal("jobsModal");
  loadJobs();
}

function openNewJobModal() {
  document.getElementById("newJobName").value = "";
  document.getElementById("newJobSource").value = `baidu:${state.baidu.path}`;
  document.getElementById("newJobDest").value = `gdrive:${state.gdrive.path}`;
  document.getElementById("newJobMode").value = "sync";
  document.getElementById("newJobInterval").value = "0";
  document.getElementById("newJobSkipExisting").checked = true;
  openModal("newJobModal");
}

async function saveNewJob() {
  const name = document.getElementById("newJobName").value.trim();
  const source = document.getElementById("newJobSource").value.trim();
  const dest = document.getElementById("newJobDest").value.trim();
  const mode = document.getElementById("newJobMode").value;
  const intervalSeconds = parseInt(document.getElementById("newJobInterval").value, 10) || 0;
  const skipExisting = document.getElementById("newJobSkipExisting").checked;

  if (!name) {
    showToast("请输入规则名称", "error");
    return;
  }
  if (!source || !dest) {
    showToast("源地址和目标地址均不可为空", "error");
    return;
  }

  try {
    const res = await fetchAPI("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        name,
        source,
        dest,
        mode,
        skip_existing: skipExisting,
        recursive: true,
        interval_seconds: intervalSeconds,
      }),
    });

    if (res.ok) {
      showToast(`同步规则「${name}」已保存`, "success");
      closeModal("newJobModal");
      loadJobs();
    } else {
      showToast(`保存失败: ${res.error}`, "error");
    }
  } catch (err) {
    showToast(`保存异常: ${err.message}`, "error");
  }
}

async function runJob(jobId) {
  try {
    const res = await fetchAPI(`/api/jobs/${jobId}/run`, { method: "POST" });
    if (res.ok) {
      showToast("已启动任务执行", "success");
      document.getElementById("taskDrawer").classList.remove("collapsed");
      loadJobs();
    } else {
      showToast(`执行失败: ${res.error}`, "error");
    }
  } catch (err) {
    showToast(`触发异常: ${err.message}`, "error");
  }
}

async function toggleJob(jobId) {
  try {
    const res = await fetchAPI(`/api/jobs/${jobId}/toggle`, { method: "POST" });
    if (res.ok) {
      showToast(`规则状态已更新为: ${res.job.status === 'active' ? '活跃' : '暂停'}`, "info");
      loadJobs();
    }
  } catch (err) {}
}

async function deleteJob(jobId) {
  if (!confirm("确定要删除此持久化同步规则吗？")) return;
  try {
    const res = await fetchAPI(`/api/jobs/${jobId}`, { method: "DELETE" });
    if (res.ok) {
      showToast("已删除同步规则", "info");
      loadJobs();
    }
  } catch (err) {}
}

function renderJobsModal() {
  const container = document.getElementById("jobsModalBody");
  if (!container) return;

  if (!state.jobs || state.jobs.length === 0) {
    container.innerHTML = `
      <div style="text-align: center; color: var(--text-muted); padding: 2rem 0; font-size: 0.9rem;">
        暂未创建任何持久化同步规则<br>
        <button class="btn btn-primary" style="margin-top: 0.75rem;" onclick="openNewJobModal()">+ 立即创建第一个同步计划</button>
      </div>`;
    return;
  }

  let html = `<div style="display: flex; flex-direction: column; gap: 0.75rem;">`;
  state.jobs.forEach(j => {
    let intervalStr = "仅手动执行";
    if (j.interval_seconds > 0) {
      if (j.interval_seconds < 3600) intervalStr = `每 ${Math.round(j.interval_seconds / 60)} 分钟`;
      else if (j.interval_seconds < 86400) intervalStr = `每 ${Math.round(j.interval_seconds / 3600)} 小时`;
      else intervalStr = `每天执行`;
    }

    const isActive = j.status === "active";
    const statusBadge = isActive
      ? `<span class="badge-gdrive">活跃中</span>`
      : `<span class="badge-baidu" style="color: #fbbf24; border-color: rgba(251, 191, 36, 0.4); background: rgba(251, 191, 36, 0.15);">已暂停</span>`;

    let lastStatusBadge = "-";
    if (j.last_status === "completed") {
      lastStatusBadge = `<span style="color: var(--accent-green);">上次成功</span>`;
    } else if (j.last_status === "failed") {
      lastStatusBadge = `<span style="color: var(--accent-red);">上次失败</span>`;
    }

    html += `
      <div style="background: rgba(15, 23, 42, 0.6); border: 1px solid var(--border-color); border-radius: var(--radius-md); padding: 0.85rem; display: flex; flex-direction: column; gap: 0.5rem;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <div style="display: flex; align-items: center; gap: 0.5rem;">
            <strong style="font-size: 0.95rem;">${escapeHtml(j.name)}</strong>
            <span class="tag-mode ${j.mode === 'sync' ? 'tag-sync' : 'tag-copy'}">${j.mode.toUpperCase()}</span>
            ${statusBadge}
          </div>
          <div style="display: flex; align-items: center; gap: 0.4rem;">
            <button class="btn btn-primary" style="padding: 0.25rem 0.55rem; font-size: 0.75rem;" onclick="runJob('${j.id}')">▶ 立即执行</button>
            <button class="btn btn-secondary" style="padding: 0.25rem 0.55rem; font-size: 0.75rem;" onclick="toggleJob('${j.id}')">${isActive ? '暂停' : '恢复'}</button>
            <button class="btn btn-danger" style="padding: 0.25rem 0.55rem; font-size: 0.75rem;" onclick="deleteJob('${j.id}')">删除</button>
          </div>
        </div>

        <div style="font-size: 0.825rem; color: var(--text-secondary); word-break: break-all;">
          ${escapeHtml(j.source)} ➔ ${escapeHtml(j.dest)}
        </div>

        <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.75rem; color: var(--text-muted); border-top: 1px solid rgba(51, 65, 85, 0.4); padding-top: 0.4rem;">
          <span>调度周期: <strong style="color: var(--text-secondary);">${intervalStr}</strong></span>
          <span>状态: ${lastStatusBadge} ${j.last_run_at ? '(上次: ' + formatTimestamp(j.last_run_at) + ')' : ''}</span>
        </div>
      </div>
    `;
  });
  html += `</div>`;
  container.innerHTML = html;
}

// Modal control
function openModal(id) {
  document.getElementById(id).classList.add("open");
}

function closeModal(id) {
  document.getElementById(id).classList.remove("open");
}

// ==========================================
// Initialization
// ==========================================
window.addEventListener("DOMContentLoaded", async () => {
  await loadStatus();
  loadFiles("baidu");
  loadFiles("gdrive");
  loadJobs();
  initTaskStream();
});
