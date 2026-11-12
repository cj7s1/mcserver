/* MCPanel plugin page. Every request is a POST to include/action.php ($.post of the Unraid web UI adds the
 * CSRF token). Text from the server is put in with textContent only. Not seen on a real Unraid server yet. */
(function () {
  'use strict';
  var URL = '/plugins/mcpanel/include/action.php';
  var facts = {};
  var view = { name: '', offset: 0, follow: false };
  var poller = null;
  var pending = null; // {task, option} waiting for the confirmation

  function $id(id) { return document.getElementById(id); }
  function show(node, yes) { node.classList.toggle('mcp-hidden', !yes); }
  function post(data) {
    return $.post(URL, data).then(function (r) { return typeof r === 'string' ? JSON.parse(r) : r; });
  }
  function when(epoch) {
    var n = parseInt(epoch, 10);
    return n > 0 ? new Date(n * 1000).toLocaleString() : '';
  }
  function newer(a, b) { return !!a && !!b && a > b; } // plugin versions compare as strings, as Unraid does

  function row(label, value, hint) {
    var tr = document.createElement('tr');
    var th = document.createElement('td');
    var td = document.createElement('td');
    th.className = 'mcp-label';
    th.textContent = label;
    td.textContent = value;
    if (hint) {
      var p = document.createElement('div');
      p.className = 'mcp-hint';
      p.textContent = hint;
      td.appendChild(p);
    }
    tr.appendChild(th);
    tr.appendChild(td);
    return tr;
  }

  function panelAddress(published) {
    // "0.0.0.0:8080", "[::]:8080", "127.0.0.1:8080" or "<address>:8080"
    var m = /^(.*):(\d+)$/.exec(published || '');
    if (!m) return null;
    var host = m[1];
    if (host === '127.0.0.1' || host === '[::1]') return { local: true, text: published };
    if (host === '0.0.0.0' || host === '[::]' || host === '') host = location.hostname;
    return { local: false, url: 'http://' + host + ':' + m[2] + '/' };
  }

  function render() {
    var f = facts, rows = $id('mcp-facts'), hint;
    rows.textContent = '';
    hint = '';
    if (newer(f.CHECKOUT_PLUGIN_VERSION, f.PLUGIN_VERSION) || newer(f.PLUGIN_UPSTREAM, f.PLUGIN_VERSION)) {
      hint = 'A newer version of this plugin exists (' + (f.CHECKOUT_PLUGIN_VERSION > (f.PLUGIN_UPSTREAM || '') ?
        f.CHECKOUT_PLUGIN_VERSION + ' in the checkout' : f.PLUGIN_UPSTREAM + ' upstream') +
        '): update it under Plugins (Check for updates), Unraid\'s normal plugin update.';
    }
    rows.appendChild(row('Plugin', f.PLUGIN_VERSION || '(not installed through the plugin manager)', hint));
    if (f.CHECKOUT) {
      rows.appendChild(row('Checkout', f.CHECKOUT + (f.CHECKOUT_FROM === 'detected' ? ' (found from the running stack)' : ' (settings)'),
        f.CHECKOUT_PROBLEM || ''));
    } else {
      rows.appendChild(row('Checkout', f.CHECKOUT_FROM === 'ambiguous' ? 'Docker knows several MCPanel stacks: set the checkout under Settings' :
        'not found: no running stack to take it from; set it under Settings'));
    }
    if (f.HEAD) {
      rows.appendChild(row('Source tree', f.HEAD + (f.BRANCH ? '  [branch ' + f.BRANCH + (f.DETACHED === '1' ? ', detached after a rollback' : '') + ']' : ''),
        f.LOCAL_CHANGES && f.LOCAL_CHANGES !== '0' ? 'Local changes in ' + f.LOCAL_CHANGES + ' file(s): Update refuses until they are committed or undone.' : ''));
    }
    rows.appendChild(row('Compose project', (f.PROJECT || '') + ' (' + ({ settings: 'settings', detected: 'found from the running stack', 'default': 'default' }[f.PROJECT_FROM] || '') + ')'));
    var notes = [];
    if (f.COMPOSE_LINK) notes.push('Linked: ' + f.COMPOSE_LINK + '.');
    if (f.LOOSE_DIRS) notes.push('Writable by group or others (mode, path): ' + f.LOOSE_DIRS + '; the boot tasks and Install Compose tighten them.');
    if (f.COMPOSE && f.COMPOSE_LINK && !f.SET_COMPOSE_BIN && !f.REMEMBERED_COMPOSE_BIN && f.OWN_COMPOSE !== 'installed') {
      notes.push('Nothing links it again after a reboot (the link is in RAM): enter that binary under Settings, or Install Compose.');
    }
    rows.appendChild(row('Docker Compose', f.COMPOSE || 'does not answer ("docker compose version")', notes.join(' ')));
    rows.appendChild(row('Shutdown hook', f.HOOK === 'installed' ? 'installed' : 'not installed (the boot tasks install it: at every Docker start, or Boot tasks > Run now)'));
    hint = '';
    if (f.BOOT_RESULT) hint = 'Last run: ' + f.BOOT_RESULT + (f.BOOT_ENDED ? ' at ' + when(f.BOOT_ENDED) : '') + ' (log ' + (f.BOOT_LOG || '').replace(/^.*\//, '') + ')';
    if (f.BOOT_NEEDS) hint += (hint ? '. ' : '') + 'NEEDS YOU: ' + f.BOOT_NEEDS;
    rows.appendChild(row('Boot tasks', f.BOOT_EVENT === 'yes' ? 'run at every Docker start' : 'the event script is missing: reinstall the plugin', hint));
    if (f.RUN_DIR_PROBLEM) rows.appendChild(row('Run folder', f.RUN_DIR_PROBLEM));
    if ((f.USER_SCRIPT || []).length || f.GO_FILE === 'yes') {
      rows.appendChild(row('Also at boot', (f.USER_SCRIPT || []).map(function (s) { return 'User Script "' + s + '"'; })
        .concat(f.GO_FILE === 'yes' ? ['/boot/config/go'] : []).join(', '),
        'It runs array-start.sh or install-hook.sh as well. The plugin does that now: take it out (both at once do no harm; they wait for each other).'));
    }
    var panel = panelAddress(f.PANEL_PUBLISHED), open = $id('mcp-open');
    if (panel && !panel.local) {
      open.href = panel.url;
      rows.appendChild(row('Panel', panel.url));
    } else if (panel) {
      rows.appendChild(row('Panel', 'listens on ' + panel.text + ' only (MCPANEL_BIND): reach it through your reverse proxy or tunnel'));
    } else {
      rows.appendChild(row('Panel', f.CHECKOUT ? 'not running' : ''));
    }
    show(open, !!(panel && !panel.local));
    var busy = f.BUSY === '1';
    if (busy) {
      rows.appendChild(row('Action', 'running: ' + (f.TASK_TASK || '') + (f.TASK_OPTION ? ' ' + f.TASK_OPTION : '') + ' since ' + when(f.TASK_STARTED)));
    } else if (f.TASK_TASK) {
      var result = f.TASK_RESULT === 'running' ? 'interrupted (its process is gone; the log shows how far it got)' : f.TASK_RESULT + (f.TASK_EXIT && f.TASK_EXIT !== '0' ? ' (exit ' + f.TASK_EXIT + ')' : '');
      rows.appendChild(row('Last action', (f.TASK_TASK || '') + (f.TASK_OPTION ? ' ' + f.TASK_OPTION : '') + ': ' + result + (f.TASK_ENDED ? ' at ' + when(f.TASK_ENDED) : ''),
        f.TASK_NEEDS ? 'NEEDS YOU: ' + f.TASK_NEEDS : ''));
    }

    // update
    var info = [];
    if (f.UPSTREAM) {
      info.push(f.BEHIND === undefined ? 'Upstream ' + f.UPSTREAM + ': not fetched yet (Check for updates).' :
        f.BEHIND + ' commit(s) behind ' + f.UPSTREAM + (f.AHEAD && f.AHEAD !== '0' ? ', ' + f.AHEAD + ' ahead (Update cannot fast-forward then: that is for the terminal)' : '') +
        (f.UPDATES_CHECKED ? ' (checked ' + when(f.UPDATES_CHECKED) + ')' : ' (not checked from this page yet)') + '.');
    }
    if (f.GIT === 'no') info.push('This checkout has no .git folder (a ZIP download): Check for updates, Update and Roll back need a git clone (docs/UNRAID.md, section 3). The boot tasks work without it.');
    $id('mcp-update-info').textContent = info.join(' ');
    var list = $id('mcp-commits');
    list.textContent = '';
    (f.UPDATE_COMMIT || []).forEach(function (c) {
      var li = document.createElement('li');
      li.textContent = c;
      list.appendChild(li);
    });
    show($id('mcp-update-run'), !busy && (f.PLAN_UPDATE === 'run' || f.PLAN_UPDATE === 'run-with-migrations'));
    show($id('mcp-migrations-row'), f.PLAN_UPDATE === 'run-with-migrations');
    show($id('mcp-rollback-run'), !busy && (f.PLAN_ROLLBACK === 'rollback' || f.PLAN_ROLLBACK === 'rollback-archive'));
    show($id('mcp-archive-row'), f.PLAN_ROLLBACK === 'rollback-archive');

    // compose
    $id('mcp-compose-info').textContent = 'The plugin\'s copy: Docker Compose ' + f.OWN_COMPOSE_VERSION + ', the official release, checked against SHA-256 ' +
      f.OWN_COMPOSE_SHA256 + ': ' + ({ installed: 'installed on the flash drive', damaged: 'on the flash drive but with ANOTHER checksum: not used, install it again', none: 'not installed' }[f.OWN_COMPOSE] || '') +
      '. A binary set under Settings is preferred while it exists.';

    // settings (not while the owner edits them)
    if (!document.activeElement || !document.activeElement.closest || !document.activeElement.closest('#mcp-settings')) {
      $id('mcp-checkout').value = f.SET_CHECKOUT || '';
      $id('mcp-project').value = f.SET_PROJECT || '';
      $id('mcp-compose-bin').value = f.SET_COMPOSE_BIN || '';
    }

    // logs
    var select = $id('mcp-logs'), keep = view.name || select.value;
    select.textContent = '';
    (f.LOG_FILE || []).forEach(function (name) {
      var o = document.createElement('option');
      o.value = name;
      o.textContent = name;
      select.appendChild(o);
    });
    if (keep) select.value = keep;

    document.querySelectorAll('#mcp input[type=button], #mcp input[type=submit]').forEach(function (b) {
      if (b.id !== 'mcp-confirm-cancel') b.disabled = busy && b.id !== 'mcp-detect';
    });
    syncCheckboxes();
  }

  function syncCheckboxes() {
    $id('mcp-run-upgrade').disabled = facts.BUSY === '1' || (facts.PLAN_UPDATE === 'run-with-migrations' && !$id('mcp-migrations').checked);
    $id('mcp-run-rollback').disabled = facts.BUSY === '1' || (facts.PLAN_ROLLBACK === 'rollback-archive' && !$id('mcp-archive').checked);
  }

  function refresh() {
    return post({ action: 'facts' }).then(function (r) {
      facts = r.facts || {};
      render();
    });
  }

  // --- the log -------------------------------------------------------------------------------------------
  function loadLog(name, follow) {
    view = { name: name, offset: 0, follow: follow };
    $id('mcp-log').textContent = '';
    poll();
  }
  function poll() {
    clearTimeout(poller);
    post({ action: 'log', name: view.name, offset: String(view.offset) }).then(function (r) {
      if (!r.ok) return;
      var pre = $id('mcp-log');
      var bottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
      if (r.name && r.name !== view.name) {
        view.name = r.name;
        pre.textContent = '';
      }
      if (r.data) pre.textContent += r.data;
      view.offset = r.offset || 0;
      if (bottom) pre.scrollTop = pre.scrollHeight;
      var t = r.task || {}, running = r.busy && (view.follow || r.current);
      $id('mcp-log-state').textContent = r.name ? (running ? 'running...' : (r.current && t.RESULT ? (t.RESULT === 'running' ? 'interrupted' : t.RESULT) : '')) : 'no log yet';
      if (running || (r.data && r.data.length >= 262144)) {
        poller = setTimeout(poll, 1000);
      } else if (view.follow) {
        view.follow = false;
        refresh();
      }
    });
  }

  // --- tasks ---------------------------------------------------------------------------------------------
  function start(task, option) {
    post({ action: 'start', task: task, option: option || '' }).then(function (r) {
      if (!r.ok) {
        $id('mcp-log-state').textContent = r.text || r.error || 'refused';
        return;
      }
      setTimeout(function () {
        refresh().then(function () { loadLog('', true); });
      }, 300);
    });
  }
  function confirmTask(task, option, title) {
    post({ action: 'describe', task: task, option: option || '' }).then(function (r) {
      pending = r.ok ? { task: task, option: option || '' } : null;
      $id('mcp-confirm-title').textContent = title;
      $id('mcp-confirm-text').textContent = r.text || r.error || '';
      show($id('mcp-confirm-run'), !!pending);
      show($id('mcp-confirm'), true);
      if ($id('mcp-confirm').scrollIntoView) $id('mcp-confirm').scrollIntoView({ block: 'center' });
    });
  }

  document.addEventListener('click', function (e) {
    var b = e.target;
    if (!b || b.tagName !== 'INPUT' || !b.closest('#mcp')) return;
    if (b.dataset.task) start(b.dataset.task, '');
    else if (b.dataset.confirm) confirmTask(b.dataset.confirm, '', b.value.replace(/\.\.\.$/, ''));
    else if (b.id === 'mcp-run-upgrade') confirmTask('update-run', $id('mcp-migrations').checked ? 'with-migrations' : '', 'Run the upgrade');
    else if (b.id === 'mcp-run-rollback') confirmTask('rollback-run', $id('mcp-archive').checked ? 'archive-other-games' : '', 'Roll back now');
    else if (b.id === 'mcp-confirm-cancel') { pending = null; show($id('mcp-confirm'), false); }
    else if (b.id === 'mcp-confirm-run' && pending) { var p = pending; pending = null; show($id('mcp-confirm'), false); start(p.task, p.option); }
    else if (b.id === 'mcp-detect') {
      post({ action: 'detect' }).then(function (r) {
        var found = (r.facts && r.facts.DETECTED) || [];
        if (found.length === 1) {
          $id('mcp-project').value = found[0].split('|')[0];
          $id('mcp-checkout').value = found[0].slice(found[0].indexOf('|') + 1);
        }
        $id('mcp-settings-msg').textContent = found.length === 1 ? 'Found; press Save to keep it.' :
          found.length ? 'Several stacks: ' + found.join(', ') : 'No running MCPanel stack found.';
      });
    }
  });
  document.addEventListener('change', function (e) {
    if (e.target.id === 'mcp-migrations' || e.target.id === 'mcp-archive') syncCheckboxes();
    if (e.target.id === 'mcp-logs') loadLog(e.target.value, false);
  });
  $id('mcp-settings').addEventListener('submit', function (e) {
    e.preventDefault();
    post({ action: 'save', checkout: $id('mcp-checkout').value, project: $id('mcp-project').value, compose_bin: $id('mcp-compose-bin').value })
      .then(function (r) {
        $id('mcp-settings-msg').textContent = r.text || r.error || '';
        refresh();
      });
  });

  refresh().then(function () { loadLog('', facts.BUSY === '1'); });
})();
