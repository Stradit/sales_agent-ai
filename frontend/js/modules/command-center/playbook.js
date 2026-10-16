import { getCommandCenter, isGenerated, markPlayTasked } from './generator.js';
import { esc, ageInDays, relativeTime } from './utils.js';
import { createTask } from './actions.js';
import { ccState, matchesCurrentAccount } from './state.js';
import { getCategoryMeta } from './feed.js';

function matchesAccount(play) {
  return matchesCurrentAccount(play.account_id, play.account_name);
}

function getPrimarySignal(item, cc) {
  const signals = (cc && cc.signals) ? cc.signals : [];
  return (
    signals.find(function(s) { return s.id && item.signal_id && String(s.id) === String(item.signal_id); }) ||
    signals.find(function(s) {
      return (s.account_id && item.account_id && String(s.account_id) === String(item.account_id)) ||
             (s.account_name && item.account_name && s.account_name.toLowerCase() === item.account_name.toLowerCase());
    })
  );
}

function itemHtml(item, cc) {
  var sig = getPrimarySignal(item, cc);
  var meta = getCategoryMeta(sig || item);
  var detectedAt = (sig && sig.detectedAt) || null;
  var isNew = (sig && sig.is_new != null)
    ? Boolean(sig.is_new)
    : (detectedAt ? (ageInDays(detectedAt) <= 4) : false);
  var timeStr = detectedAt ? relativeTime(detectedAt) : 'This week';

  var newBadge = isNew
    ? '<span class="cc-sig-new-badge"><span class="cc-pulse-dot"></span>NEW</span>'
    : '';

  var tasked = !!item.tasked;
  var btnCls = tasked ? 'cc-btn-done' : 'cc-btn-primary';
  var btnHtml = tasked
    ? '<i class="fa-solid fa-check"></i> Tasked'
    : '<i class="fa-solid fa-plus"></i> Create task';

  return '<li class="cc-signal-card cc-pb-play-card' + (isNew ? ' is-new' : '') + (tasked ? ' cc-pb-tasked' : '') + '" data-rank="' + item.rank + '">' +
    '<div class="cc-sig-card-header">' +
      '<div class="cc-sig-badge-group">' +
        '<span class="cc-pb-rank-badge">#' + String(item.rank).padStart(2, '0') + '</span>' +
        '<span class="cc-sig-cat-badge ' + meta.cls + '"><i class="' + meta.icon + '"></i> ' + esc(meta.label) + '</span>' +
        '<span class="cc-sig-account-badge" title="' + esc(item.account_name) + '"><i class="fa-regular fa-building"></i> ' + esc(item.account_name) + '</span>' +
        '<span class="cc-sig-time-badge' + (isNew ? ' is-recent' : '') + '"><i class="fa-regular fa-clock"></i> ' + esc(timeStr) + '</span>' +
        newBadge +
      '</div>' +
      '<div class="cc-sig-action-wrap">' +
        '<button type="button" class="cc-btn ' + btnCls + ' cc-btn-xs cc-sig-task-btn" data-rank="' + item.rank + '" title="Create task for ' + esc(item.account_name) + '"' + (tasked ? ' disabled' : '') + '>' +
          btnHtml +
        '</button>' +
      '</div>' +
    '</div>' +
    '<div class="cc-sig-title-row">' +
      '<span class="cc-sig-title-text" title="' + esc(item.title) + '">' + esc(item.title) + '</span>' +
    '</div>' +
  '</li>';
}

export function renderPlaybook() {
  var list = document.getElementById('ccPlaybookList');
  if (!list) return;

  if (!isGenerated()) {
    list.innerHTML = '<li class="cc-feed-empty">Not generated yet \u2014 press <strong>Generate</strong> to build this week\'s plays from the top signals.</li>';
    return;
  }

  var cc = getCommandCenter();
  var playbook = cc.playbook || [];

  if (!playbook.length) {
    list.innerHTML = '<li class="cc-feed-empty">No plays this week \u2014 there were no signals in the last 7 days.</li>';
    return;
  }

  var actions = playbook.filter(matchesAccount);
  if (!actions.length) {
    list.innerHTML = '<li class="cc-feed-empty">No plays currently generated for the selected account.</li>';
    return;
  }

  list.innerHTML = actions.map(function(item) { return itemHtml(item, cc); }).join('');

  // Create Task buttons
  list.querySelectorAll('button[data-rank]').forEach(function(btn) {
    btn.addEventListener('click', async function(e) {
      e.stopPropagation();
      var rankNum = Number(btn.dataset.rank);
      var item = playbook.find(function(p) { return p.rank === rankNum; });
      if (!item || item.tasked) return;

      btn.disabled = true;
      var origHtml = btn.innerHTML;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Creating...';

      try {
        var ok = await createTask(item.account_name, item.title, {
          accountId:   item.account_id,
          description: item.rationale,
          priority:    item.impact || 'high',
          source:      'playbook',
        });
        if (ok) {
          markPlayTasked(item);
          btn.innerHTML = '<i class="fa-solid fa-check"></i> Tasked';
          btn.classList.remove('cc-btn-primary');
          btn.classList.add('cc-btn-done');
          document.dispatchEvent(new CustomEvent('cc:plays-changed'));
        } else {
          btn.disabled = false;
          btn.innerHTML = origHtml;
        }
      } catch(_) {
        btn.disabled = false;
        btn.innerHTML = origHtml;
      }
    });
  });
}

