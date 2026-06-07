function toggleTheme() {
  var html = document.documentElement;
  var current = html.getAttribute('data-theme');
  var next = current === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  localStorage.setItem('botfarm-theme', next);
  var themeBtns = document.querySelectorAll('.theme-btn');
  themeBtns.forEach(function(b) {
    b.textContent = next === 'dark' ? '🌙' : '☀️';
  });
  if (typeof updateChartsTheme === 'function') {
    updateChartsTheme();
  }
}

function initTheme() {
  var saved = localStorage.getItem('botfarm-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  var btn = document.getElementById('themeBtn');
  if (btn) {
    btn.textContent = saved === 'dark' ? '🌙' : '☀️';
  }
  var themeBtns = document.querySelectorAll('.theme-btn');
  themeBtns.forEach(function(b) {
    b.textContent = saved === 'dark' ? '🌙' : '☀️';
  });
}

let modalCallback = null;
function showModal(title, text, cb) {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-text').textContent = text;
  modalCallback = cb;
  
  var modal = document.getElementById('modal');
  modal.style.display = 'flex';
  setTimeout(function() {
    modal.classList.add('show');
  }, 10);
}

function confirmModal() {
  closeModal();
  if (typeof modalCallback === 'function') {
    modalCallback();
    modalCallback = null;
  }
}

function closeModal() {
  var modal = document.getElementById('modal');
  modal.classList.remove('show');
  setTimeout(function() {
    modal.style.display = 'none';
  }, 250);
}

function toast(msg, type) {
  var t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = (type === 'success' ? '✓ ' : '✗ ') + msg;
  t.className = 'toast ' + type + ' show';
  setTimeout(function() {
    t.classList.remove('show');
  }, 3500);
}

function fmtDateTime(isoString) {
  if (!isoString) return '-';
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    
    const now = new Date();
    const isToday = d.getDate() === now.getDate() && 
                    d.getMonth() === now.getMonth() && 
                    d.getFullYear() === now.getFullYear();
                    
    const pad = (n) => String(n).padStart(2, '0');
    const hh = pad(d.getHours());
    const mm = pad(d.getMinutes());
    const ss = pad(d.getSeconds());
    const timeStr = `${hh}:${mm}:${ss}`;
    
    if (isToday) {
      return timeStr;
    } else {
      const year = d.getFullYear();
      const month = pad(d.getMonth() + 1);
      const date = pad(d.getDate());
      return `${year}-${month}-${date} ${timeStr}`;
    }
  } catch (e) {
    return isoString;
  }
}

function changeAccountStatus(accountName, status, botName, callback) {
  fetch('/api/action/account', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: accountName, status: status, bot: botName })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.ok) {
      toast('Account "' + accountName + '" status updated to ' + status, 'success');
      if (typeof callback === 'function') callback();
    } else {
      toast(d.error || 'Failed to update status', 'error');
    }
  })
  .catch(function() {
    toast('Network error setting status', 'error');
  });
}

function loadSidebarBots(activeBotName) {
  fetch('/api/overview')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok && d.bots) {
        var listContainer = document.getElementById('sidebar-bots-list');
        if (listContainer) {
          listContainer.innerHTML = '';
          d.bots.forEach(function(b) {
            var activeClass = b === activeBotName ? 'active' : '';
            listContainer.innerHTML += '<a href="/bot/' + encodeURIComponent(b) + '" class="' + activeClass + '">🤖 ' + esc(b) + '</a>';
          });
        }
      }
    })
    .catch(function() {});
}

function esc(s) {
  if (!s) return '';
  return s.toString().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtType(t) {
  return t.replace(/_/g,' ').replace(/\b\w/g, function(c){return c.toUpperCase()});
}

function toggleSidebar() {
  var sidebar = document.querySelector('.sidebar');
  var overlay = document.getElementById('sidebarOverlay');
  var isOpen = sidebar.classList.contains('open');
  if (isOpen) {
    closeSidebar();
  } else {
    sidebar.classList.add('open');
    if (overlay) overlay.classList.add('show');
    document.body.classList.add('sidebar-open');
  }
}

function closeSidebar() {
  var sidebar = document.querySelector('.sidebar');
  var overlay = document.getElementById('sidebarOverlay');
  sidebar.classList.remove('open');
  if (overlay) overlay.classList.remove('show');
  document.body.classList.remove('sidebar-open');
}
