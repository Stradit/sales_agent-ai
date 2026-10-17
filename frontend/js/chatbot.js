$(function () {

  const $fab = $('#chatbotFab');
  const $panel = $('#chatbotPanel');
  const $messages = $('#chatbotMessages');
  const $subtitle = $('#chatbotSubtitle');
  const $form = $('#chatbotForm');
  const $input = $('#chatbotInput');

  let greeted = false;

  // Escapes for HTML text AND attribute values (quotes too: many callers build href="${esc(x)}").
  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function getContext() {
    if (typeof window.getSalesAssistantContext === 'function') {
      return window.getSalesAssistantContext();
    }
    return { accounts: [], account: null, lob: null, persona: null };
  }

  function scrollToBottom() {
    $messages.stop().animate({ scrollTop: $messages[0].scrollHeight }, 200);
  }

  function addMessage(role, html) {
    const iconHtml = role === 'bot' ? '<div class="chatbot-msg-icon">🤖</div>' : '';
    $messages.append(`
      <div class="chatbot-msg chatbot-msg-${role}">
        ${iconHtml}
        <div class="chatbot-msg-bubble">${html}</div>
      </div>
    `);
    scrollToBottom();
  }

  function updateSubtitle() {
    const ctx = getContext();
    $subtitle.text(ctx.account ? ctx.account.name : 'Account Intelligence');
  }

  function listPersonas(ctx) {
    if (ctx.lob) {
      const direct = ctx.lob.personas || [];
      const sub = (ctx.lob.subLobs || []).flatMap(s => s.personas || []);
      return [...direct, ...sub];
    }
    return (ctx.account.lobs || []).flatMap(l => {
      const direct = l.personas || [];
      const sub = (l.subLobs || []).flatMap(s => s.personas || []);
      return [...direct, ...sub];
    });
  }

  function generateReply(text) {
    const ctx = getContext();
    const q = (text || '').toLowerCase();

    if (!ctx.account) {
      if (ctx.accounts && ctx.accounts.length) {
        const names = ctx.accounts.slice(0, 5).map(a => `• ${esc(a.name)}`).join('<br>');
        return `Select an account from the sidebar first. You have ${ctx.accounts.length} account${ctx.accounts.length !== 1 ? 's' : ''} loaded, including:<br>${names}<br><br>Once selected, ask me for a summary, key personas, or lines of business.`;
      }
      return `No account data is loaded yet. Try "Run Pipeline" from the sidebar, then ask me again.`;
    }

    const a = ctx.account;

    if (/summar|overview|about this account|tell me about/.test(q)) {
      const lobCount = (a.lobs || []).length;
      return `<strong>${esc(a.name)}</strong> (${esc(a.ticker || 'Enterprise')})<br>` +
        `${esc(a.revenue || 'Revenue N/A')} • ${esc(a.location || 'Location N/A')}<br>` +
        `${esc(a.desc || 'No description available.')}<br><br>` +
        `Tracking ${lobCount} line${lobCount !== 1 ? 's' : ''} of business.`;
    }

    if (/persona|contact|people|who\b|stakeholder/.test(q)) {
      const personas = listPersonas(ctx);
      if (!personas.length) {
        return ctx.lob
          ? `No personas mapped yet for ${esc(ctx.lob.name)}.`
          : `No personas mapped yet for ${esc(a.name)}. Open a Line of Business card to check for contacts there.`;
      }
      const scope = ctx.lob ? ` in ${esc(ctx.lob.name)}` : ` across ${esc(a.name)}`;
      const rows = personas.slice(0, 6).map(p =>
        `<li>${esc(p.name)} — ${esc(p.title || 'Executive')}${p.tier ? ` (${esc(p.tier)})` : ''}</li>`
      ).join('');
      const more = personas.length > 6 ? `<br><em>+${personas.length - 6} more</em>` : '';
      return `${personas.length} persona${personas.length !== 1 ? 's' : ''} found${scope}:<ul>${rows}</ul>${more}`;
    }

    if (/lob\b|division|business line|line of business/.test(q)) {
      const lobs = a.lobs || [];
      if (!lobs.length) return `No lines of business have been discovered yet for ${esc(a.name)}.`;
      const rows = lobs.map(l => {
        const count = (l.personas || []).length + (l.subLobs || []).flatMap(s => s.personas || []).length;
        return `<li>${esc(l.name)} — ${esc(l.revenue || l.desc || 'Business Division')} (${count} contact${count !== 1 ? 's' : ''})</li>`;
      }).join('');
      return `${lobs.length} line${lobs.length !== 1 ? 's' : ''} of business for ${esc(a.name)}:<ul>${rows}</ul>`;
    }

    if (/most contact/.test(q)) {
      const lobs = a.lobs || [];
      if (!lobs.length) return `No lines of business found for ${esc(a.name)}.`;
      let best = null, bestCount = -1;
      lobs.forEach(l => {
        const count = (l.personas || []).length + (l.subLobs || []).flatMap(s => s.personas || []).length;
        if (count > bestCount) { bestCount = count; best = l; }
      });
      return best ? `<strong>${esc(best.name)}</strong> has the most contacts mapped (${bestCount}).` : `Couldn't determine that yet.`;
    }

    if (/icebreaker|outreach|opening line|email/.test(q)) {
      if (ctx.persona) {
        return ctx.persona.personalized_icebreaker
          ? `"${esc(ctx.persona.personalized_icebreaker)}"`
          : `No personalized icebreaker generated yet for ${esc(ctx.persona.name)}. Try fetching persona intel from their detail panel.`;
      }
      return `Open a persona's detail panel to get a tailored icebreaker, or ask "Who are the key personas?" to see who's available at ${esc(a.name)}.`;
    }

    if (/linkedin/.test(q)) {
      if (ctx.persona && ctx.persona.linkedin_url) {
        return `<a href="${esc(ctx.persona.linkedin_url)}" target="_blank">Open ${esc(ctx.persona.name)}'s LinkedIn ↗</a>`;
      }
      return `Select a persona first — I'll surface their LinkedIn profile if one's on file.`;
    }

    return `I can help with <strong>${esc(a.name)}</strong>. Try asking:<br>` +
      `<ul><li>Give me a summary of this account</li><li>Who are the key personas?</li><li>What lines of business are active?</li><li>Give me an icebreaker</li></ul>`;
  }

  function handleQuery(text) {
    if (!text || !text.trim()) return;
    addMessage('user', esc(text));
    $input.val('');
    setTimeout(() => addMessage('bot', generateReply(text)), 260);
  }

  function openPanel() {
    updateSubtitle();
    if (!greeted) {
      addMessage('bot', `Hi — I'm your Sales Assistant. Select an account and ask me for a summary, key personas, or lines of business.`);
      greeted = true;
    }
    $panel.addClass('open');
    $fab.addClass('open');
    setTimeout(() => $input.trigger('focus'), 150);
  }

  function closePanel() {
    $panel.removeClass('open');
    $fab.removeClass('open');
  }

  $fab.on('click', function () {
    if ($panel.hasClass('open')) closePanel();
    else openPanel();
  });

  $('#chatbotClose').on('click', closePanel);

  $form.on('submit', function (e) {
    e.preventDefault();
    handleQuery($input.val());
  });

  $(document).on('click', '.chatbot-chip, .chatbot-suggestion', function () {
    handleQuery($(this).data('query'));
  });

});
