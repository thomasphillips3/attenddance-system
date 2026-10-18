/* Renders a /api/tap result as the full-screen card staff read at arm's
 * length, and wires its buttons (undo, put back in class, pick a class).
 * Everything goes through textContent, so names can't inject markup.
 *
 *   TapScreen.render(el, result, { onUndo, onReopen, onPick })
 *   TapScreen.message(el, tone, word, title, detail, buttons)
 */
(function () {
    const TONE = {
        checked_in: 'green', already_in: 'green', reopened: 'green',
        checked_out: 'blue',
        needs_class: 'amber', already_out: 'amber', inactive_student: 'amber',
        unknown_tag: 'red', inactive_tag: 'red', unassigned_tag: 'red',
        undone: 'grey',
    };
    const WORD = {
        checked_in: 'IN', already_in: 'IN', reopened: 'IN',
        checked_out: 'OUT', already_out: 'OUT',
        needs_class: 'PICK', inactive_student: '!',
        unknown_tag: 'NO', inactive_tag: 'NO', unassigned_tag: 'NO',
    };

    function el(tag, cls, text) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }

    function clock(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d)) return '';
        return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }

    function to12(hhmm) {
        const p = String(hhmm || '').split(':');
        const hr = Number(p[0]), mi = Number(p[1]);
        if (isNaN(hr) || isNaN(mi)) return hhmm || '';
        return (hr % 12 || 12) + ':' + String(mi).padStart(2, '0') + ' ' + (hr >= 12 ? 'PM' : 'AM');
    }

    function shown(r) {
        return r.outcome === 'duplicate' && r.previous_outcome ? r.previous_outcome : r.outcome;
    }

    const TapScreen = {};

    TapScreen.tone = function (r) { return TONE[shown(r)] || 'red'; };

    TapScreen.render = function (root, r, h) {
        h = h || {};
        const o = shown(r);
        root.className = 'tap-screen tone-' + (TONE[o] || 'red');
        root.replaceChildren();
        root.appendChild(el('div', 'tap-word', WORD[o] || '!'));
        if (r.photo_url) {
            const img = el('img', 'tap-photo');
            img.alt = '';
            img.src = r.photo_url;
            img.addEventListener('error', () => img.remove());
            root.appendChild(img);
        }
        if (r.student) {
            const last = r.student.last_name ? ' ' + r.student.last_name.charAt(0) + '.' : '';
            root.appendChild(el('div', 'tap-name', r.student.first_name + last));
        }
        root.appendChild(el('div', 'tap-msg', r.message || ''));
        const bits = [];
        if (r.occurred_at) bits.push(clock(r.occurred_at));
        if (r.tag_label) bits.push(r.tag_label);
        if (r.outcome === 'duplicate') bits.push('repeat tap ignored');
        if (r.is_tardy && (o === 'checked_in')) bits.push('marked late');
        if (bits.length) root.appendChild(el('div', 'tap-meta', bits.join(' · ')));
        if (r.rolled_from && r.class && r.rolled_from.id !== r.class.id) {
            root.appendChild(el('div', 'tap-note', r.rolled_from.name + ' was closed at its end time.'));
        }
        if (r.flags && r.flags.length) {
            const f = el('div', 'tap-flags');
            r.flags.forEach(x => f.appendChild(el('span', 'tap-flag', x.label)));
            root.appendChild(f);
        }
        if (r.candidates && r.candidates.length && h.onPick) {
            root.appendChild(el('div', 'tap-picker-title',
                o === 'needs_class' ? 'Check in to which class?' : 'Or check in to another class:'));
            const p = el('div', 'tap-picker');
            r.candidates.slice(0, 8).forEach(c => {
                const b = el('button', 'tap-btn tap-btn-light', c.name);
                b.appendChild(el('small', '', to12(c.start_time) + ' to ' + to12(c.end_time)
                    + (c.enrolled ? '' : ' · not enrolled')));
                b.addEventListener('click', () => h.onPick(c, b));
                p.appendChild(b);
            });
            root.appendChild(p);
        }
        const actions = el('div', 'tap-actions');
        if (r.can_undo && h.onUndo) {
            const b = el('button', 'tap-btn tap-btn-ghost', 'Undo');
            b.addEventListener('click', () => h.onUndo(b));
            actions.appendChild(b);
        }
        if (r.can_reopen && h.onReopen) {
            const b = el('button', 'tap-btn tap-btn-ghost', 'Put back in class');
            b.addEventListener('click', () => h.onReopen(b));
            actions.appendChild(b);
        }
        (h.extra || []).forEach(x => actions.appendChild(x));
        if (actions.children.length) root.appendChild(actions);
    };

    // A non-result screen: recording, not recorded, sign in, setup prompt.
    TapScreen.message = function (root, tone, word, title, detail, buttons) {
        root.className = 'tap-screen tone-' + tone;
        root.replaceChildren();
        if (word === 'spinner') root.appendChild(el('div', 'tap-spinner'));
        else if (word) root.appendChild(el('div', 'tap-word', word));
        if (title) root.appendChild(el('div', 'tap-name', title));
        if (detail) root.appendChild(el('div', 'tap-msg', detail));
        if (buttons && buttons.length) {
            const a = el('div', 'tap-actions');
            buttons.forEach(b => a.appendChild(b));
            root.appendChild(a);
        }
    };

    TapScreen.button = function (label, cls, onClick) {
        const b = el('button', 'tap-btn ' + (cls || 'tap-btn-light'), label);
        b.addEventListener('click', () => onClick(b));
        return b;
    };

    TapScreen.link = function (label, href, cls) {
        const a = el('a', 'tap-btn ' + (cls || 'tap-btn-light'), label);
        a.href = href;
        a.style.textDecoration = 'none';
        return a;
    };

    window.TapScreen = TapScreen;
})();
