// Checkbox list of a dancer's open charges for the payment forms.
//
// Each checked charge gets its own amount, so one payment can pay several
// charges, and a part can be less than the charge (a partial payment). The
// parts can add up to less than the payment: the rest is unapplied and pays
// the oldest open charges, same as a payment with nothing checked.
//
// Used by the Billing page modal and the dancer ledger modal. The server
// re-checks everything (POST/PUT /api/transactions); this only helps the
// person typing see the numbers before they save.
(function () {
    const FUND_NAMES = { studio: 'Studio', company: 'Company' };
    const money = n => '$' + Number(n).toFixed(2);
    const cents = v => Math.round((parseFloat(v) || 0) * 100);
    // Escapes quotes too: descriptions land in attribute values (aria-label).
    function esc(str) {
        return String(str == null ? '' : str).replace(/[&<>"']/g, c =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    // The most that can be linked to a charge. Links outrank oldest-first
    // money, so this can be more than what's left (see charge_remainders).
    const room = c => (c.link_room === undefined ? c.remaining : c.link_room);

    // opts.list: element the rows render into
    // opts.amount: the payment amount <input>
    // opts.summary: element for the "Applying $X..." line
    // opts.onChange(picker): called after any check or amount change
    window.ChargePicker = function (opts) {
        let charges = [];
        let fundFilter = '';
        // While true the payment amount follows the sum of the checked parts.
        // Typing in the amount box switches that off.
        let autoAmount = true;
        // 'idle' | 'loading' | 'ready' | 'failed'. Saving waits for 'ready' so
        // an unfinished list never sends an empty split that unlinks charges.
        let state = 'idle';
        // Bumped by every load and clear; a response for an older load is dropped.
        let loadSeq = 0;

        opts.amount.addEventListener('input', () => { autoAmount = false; refresh(); });

        function rows() { return Array.from(opts.list.querySelectorAll('[data-charge-row]')); }
        function checkedRows() { return rows().filter(r => r.querySelector('input[type=checkbox]').checked); }
        function chargeFor(row) { return charges.find(c => c.id === Number(row.dataset.chargeRow)); }

        function render(preset) {
            const presetBy = Object.fromEntries((preset || []).map(a => [Number(a.charge_id), a.amount]));
            const shown = charges.filter(c => !fundFilter || c.fund === fundFilter);
            if (!shown.length) {
                opts.list.innerHTML = '<p class="px-3 py-3 text-sm text-ink-3">No unpaid charges. The payment will sit as a credit until a charge is added.</p>';
                refresh();
                return;
            }
            opts.list.innerHTML = shown.map(c => {
                const pre = presetBy[c.id];
                const fundTag = fundFilter ? '' : ` &middot; <span class="${c.fund === 'company' ? 'text-gold-ink font-bold' : ''}">${esc(FUND_NAMES[c.fund] || c.fund)}</span>`;
                return `<div data-charge-row="${c.id}" class="flex items-center gap-3 px-3 py-2 border-b border-line last:border-0">
                    <input type="checkbox" id="cp-${c.id}" class="h-4 w-4 accent-primary-500" aria-label="Apply to ${esc(c.description || c.category)}" ${pre !== undefined ? 'checked' : ''}>
                    <label for="cp-${c.id}" class="flex-1 min-w-0 cursor-pointer">
                        <span class="block text-sm text-ink truncate">${esc(c.description || c.category)}</span>
                        <span class="block text-xs text-ink-3 tabular-nums">${esc(c.date)} &middot; ${money(Math.max(c.remaining, 0))} left of ${money(c.amount)}${fundTag}</span>
                    </label>
                    <div class="relative w-28 shrink-0">
                        <span class="absolute inset-y-0 left-0 pl-2 flex items-center text-ink-3 text-sm pointer-events-none">$</span>
                        <input type="number" step="0.01" min="0.01" max="${room(c).toFixed(2)}" aria-label="Amount toward ${esc(c.description || c.category)}"
                            value="${pre !== undefined ? Number(pre).toFixed(2) : ''}" ${pre !== undefined ? '' : 'disabled'}
                            class="block w-full pl-5 pr-2 py-1.5 bg-surface border border-line-2 rounded-lg text-sm text-right tabular-nums disabled:bg-surface-2 disabled:text-ink-3 focus:outline-none focus:ring-2 focus:ring-primary-200">
                    </div>
                </div>`;
            }).join('');
            rows().forEach(row => {
                const box = row.querySelector('input[type=checkbox]');
                const part = row.querySelector('input[type=number]');
                box.addEventListener('change', () => {
                    part.disabled = !box.checked;
                    if (box.checked) {
                        const c = chargeFor(row);
                        // A typed payment amount caps what a newly checked charge
                        // takes, so $100 against a $200 charge fills in $100.
                        const free = autoAmount ? Infinity : (cents(opts.amount.value) - allocatedCents()) / 100;
                        const owed = c.remaining > 0 ? c.remaining : room(c);
                        part.value = Math.max(0, Math.min(owed, free)).toFixed(2);
                        if (cents(part.value) === 0) part.value = owed.toFixed(2);
                    } else {
                        part.value = '';
                    }
                    refresh();
                });
                part.addEventListener('input', refresh);
            });
            refresh();
        }

        function allocatedCents() {
            return checkedRows().reduce((sum, r) => sum + cents(r.querySelector('input[type=number]').value), 0);
        }

        // Charges in the other fund can't be mixed into the same payment.
        function lockFunds() {
            const fund = picker.fund();
            rows().forEach(row => {
                const box = row.querySelector('input[type=checkbox]');
                const other = fund && chargeFor(row).fund !== fund;
                box.disabled = other;
                row.classList.toggle('opacity-40', other);
            });
        }

        function refresh() {
            lockFunds();
            const applied = allocatedCents();
            if (autoAmount && checkedRows().length) opts.amount.value = (applied / 100).toFixed(2);
            const err = picker.error();
            rows().forEach(row => {
                const part = row.querySelector('input[type=number]');
                const over = !part.disabled && cents(part.value) > Math.round(room(chargeFor(row)) * 100);
                part.classList.toggle('border-danger', over);
            });
            const total = cents(opts.amount.value);
            const n = checkedRows().length;
            let text = '';
            if (err) {
                text = `<span class="text-danger font-medium">${esc(err)}</span>`;
            } else if (n) {
                const left = total - applied;
                text = `Applying ${money(applied / 100)} to ${n} charge${n === 1 ? '' : 's'}.`
                    + (left > 0 ? ` ${money(left / 100)} left over pays the oldest unpaid charges.` : '');
            } else {
                text = 'Nothing checked: the payment pays the oldest unpaid charges first.';
            }
            opts.summary.innerHTML = text;
            if (opts.onChange) opts.onChange(picker);
        }

        const picker = {
            // Fetch the open charges. excludePayment lists what each charge owed
            // before that payment, for editing it; preset re-checks its split.
            // keepAmount: the amount box already holds a real amount (an edit),
            // so don't let checked charges overwrite it. Switching dancers
            // keeps whatever the person already typed.
            async load(studentId, { excludePayment = null, preset = [], keepAmount = false } = {}) {
                const seq = ++loadSeq;
                charges = [];
                if (keepAmount) autoAmount = false;
                if (!studentId) {
                    state = 'idle';
                    opts.list.innerHTML = '<p class="px-3 py-3 text-sm text-ink-3">Pick a dancer to see their charges.</p>';
                    refresh();
                    return;
                }
                state = 'loading';
                opts.list.innerHTML = '<p class="px-3 py-3 text-sm text-ink-3">Loading charges...</p>';
                const url = `/api/students/${encodeURIComponent(studentId)}/open-charges` + (excludePayment ? `?exclude_payment=${encodeURIComponent(excludePayment)}` : '');
                let loaded;
                try {
                    const res = await fetch(url);
                    if (!res.ok) throw new Error('Could not load charges');
                    loaded = (await res.json()).charges;
                } catch (err) {
                    if (seq !== loadSeq) return;
                    state = 'failed';
                    opts.list.innerHTML = '<p class="px-3 py-3 text-sm text-danger">Could not load charges. Close and reopen the form to try again.</p>';
                    return;
                }
                if (seq !== loadSeq) return;  // a newer load or a clear() won
                charges = loaded;
                state = 'ready';
                render(preset);
            },
            clear() { loadSeq++; state = 'idle'; charges = []; autoAmount = true; opts.list.innerHTML = ''; opts.summary.innerHTML = ''; },
            // True once the charge list has loaded, so allocations() is the
            // person's real choice and safe to send.
            ready() { return state === 'ready'; },
            loading() { return state === 'loading'; },
            setFund(fund) {
                if (fund === fundFilter) return;
                fundFilter = fund || '';
                render([]);
            },
            // The fund of the checked charges, or '' when none are checked.
            fund() {
                const row = checkedRows()[0];
                return row ? chargeFor(row).fund : '';
            },
            // The single category every checked charge shares, else ''.
            category() {
                const cats = new Set(checkedRows().map(r => chargeFor(r).category));
                return cats.size === 1 ? [...cats][0] : '';
            },
            allocations() {
                return checkedRows().map(r => ({
                    charge_id: Number(r.dataset.chargeRow),
                    amount: (cents(r.querySelector('input[type=number]').value) / 100).toFixed(2),
                }));
            },
            error() {
                for (const row of checkedRows()) {
                    const c = chargeFor(row);
                    const part = cents(row.querySelector('input[type=number]').value);
                    if (part <= 0) return `Enter an amount for ${c.description || c.category}.`;
                    if (part > Math.round(room(c) * 100)) return `${c.description || c.category} has only ${money(room(c))} left.`;
                }
                const applied = allocatedCents();
                const total = cents(opts.amount.value);
                if (checkedRows().length && applied > total) {
                    return `The checked charges add up to ${money(applied / 100)}, more than the ${money(total / 100)} payment.`;
                }
                return null;
            },
        };
        return picker;
    };
})();
