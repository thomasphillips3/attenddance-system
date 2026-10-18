/* Tap station: dancers tap one after another, no banner, no typing.
 *
 * Inputs:
 *   - Web NFC (Chrome on Android): continuous scan after one Start tap.
 *   - USB keyboard-wedge reader (any device): the reader "types" the chip
 *     serial and presses Enter. Caught at the document level, so no input
 *     box has to be focused.
 *
 * Offline: a tap that can't reach the server goes into an IndexedDB queue
 * with the time it happened, and is replayed in order through
 * /api/tap/batch once the station is back online. Each tap carries its own
 * event_key, so a retried send never records twice. The queue holds only
 * the tag's code or serial and a time - never a name - and empties as soon
 * as the server has it.
 */
(function () {
    const screen = document.getElementById('screen');
    const netChip = document.getElementById('net');
    const queueChip = document.getElementById('queue');
    const modeChip = document.getElementById('mode');

    const RESULT_MS = 4500;       // plain result stays up this long
    const ACTION_MS = 12000;      // result with Undo / Put back in class
    const PICK_MS = 25000;        // class picker
    const LOCAL_REPEAT_MS = 2500; // a fob resting on the phone reads over and over

    let audio = null, wakeLock = null, nfcAbort = null, resetTimer = null;
    let current = null, signedOut = false, flushing = false, started = false;
    let usbSeen = false, nfcOn = false;
    const lastSeen = new Map();

    // ── IndexedDB queue ──────────────────────────────────────────────
    const DB_NAME = 'tap-station', STORE = 'taps';
    let dbp = null;
    function db() {
        if (!dbp) {
            dbp = new Promise((resolve, reject) => {
                const r = indexedDB.open(DB_NAME, 1);
                r.onupgradeneeded = () => r.result.createObjectStore(STORE, { keyPath: 'event_key' });
                r.onsuccess = () => resolve(r.result);
                r.onerror = () => reject(r.error);
            });
        }
        return dbp;
    }
    async function tx(mode, fn) {
        const d = await db();
        return new Promise((resolve, reject) => {
            const t = d.transaction(STORE, mode);
            const store = t.objectStore(STORE);
            let out;
            Promise.resolve(fn(store)).then(v => { out = v; });
            t.oncomplete = () => resolve(out);
            t.onerror = () => reject(t.error);
        });
    }
    const qAdd = (item) => tx('readwrite', s => { s.put(item); });
    const qDel = (keys) => tx('readwrite', s => { keys.forEach(k => s.delete(k)); });
    const qAll = () => tx('readonly', s => new Promise(res => {
        const r = s.getAll();
        r.onsuccess = () => res((r.result || []).sort((a, b) => a.occurred_at - b.occurred_at));
    }));
    async function qCount() {
        try { return (await qAll()).length; } catch (e) { return 0; }
    }

    // ── Status chips ─────────────────────────────────────────────────
    function setNet(state) {
        netChip.className = 'chip ' + ({ online: 'ok', offline: 'off', signin: 'off' }[state] || '');
        netChip.textContent = { online: 'Online', offline: 'Offline', signin: 'Signed out' }[state] || '…';
    }
    async function updateQueueChip() {
        const n = await qCount();
        queueChip.hidden = n === 0;
        queueChip.textContent = n + ' saved, not sent';
    }
    function updateModeChip() {
        const bits = [];
        if (nfcOn) bits.push('NFC on');
        if (usbSeen) bits.push('USB reader');
        modeChip.textContent = bits.join(' + ') || (started ? 'Waiting for a tag' : '');
        modeChip.hidden = !modeChip.textContent;
    }

    // ── Sound and buzz ───────────────────────────────────────────────
    function canBuzz() {
        // Chrome blocks vibrate() until the user has touched the page.
        return navigator.vibrate && (!navigator.userActivation || navigator.userActivation.hasBeenActive);
    }
    function tone(kind) {
        if (canBuzz()) {
            try { navigator.vibrate(kind === 'err' ? [70, 90, 70] : 80); } catch (e) { }
        }
        if (!audio) return;
        const seq = {
            in: [[880, 0.09], [1320, 0.14]],
            out: [[1320, 0.09], [880, 0.14]],
            err: [[220, 0.22], [175, 0.28]],
            queued: [[660, 0.14]],
        }[kind] || [[660, 0.1]];
        let t = audio.currentTime + 0.01;
        for (const [freq, dur] of seq) {
            const o = audio.createOscillator(), g = audio.createGain();
            o.type = kind === 'err' ? 'square' : 'sine';
            o.frequency.value = freq;
            g.gain.setValueAtTime(0.0001, t);
            g.gain.exponentialRampToValueAtTime(0.35, t + 0.01);
            g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
            o.connect(g).connect(audio.destination);
            o.start(t); o.stop(t + dur + 0.02);
            t += dur + 0.03;
        }
    }
    function toneFor(r) {
        const t = TapScreen.tone(r);
        return t === 'green' ? 'in' : t === 'blue' ? 'out' : 'err';
    }

    // ── Screens ──────────────────────────────────────────────────────
    function el(tag, cls, text) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text) n.textContent = text;
        return n;
    }
    function ready() {
        clearTimeout(resetTimer);
        current = null;
        screen.className = 'tap-screen tone-idle';
        screen.replaceChildren(el('div', 'ready-word', 'Tap your tag'),
            el('div', 'hint', nfcOn ? 'Hold your key fob or card to the back of this phone.'
                : 'Hold your key fob or card to the reader.'));
    }
    function later(ms) { clearTimeout(resetTimer); resetTimer = setTimeout(ready, ms); }

    function startScreen() {
        const hint = TapNFC.supported()
            ? 'Tap Start, then allow NFC. Keep this page open during drop-off.'
            : "This device can't read tags by itself. Plug in the USB reader, then tap Start to turn on sounds.";
        screen.className = 'tap-screen tone-idle';
        const b = el('button', 'tap-btn tap-btn-primary', 'Start');
        b.style.fontSize = '1.4rem'; b.style.padding = '1rem 3rem';
        b.addEventListener('click', start);
        screen.replaceChildren(el('div', 'ready-word', 'Tap station'), el('div', 'hint', hint), el('div', 'tap-actions'));
        screen.lastChild.appendChild(b);
    }

    function showResult(r) {
        current = r;
        TapScreen.render(screen, r, { onUndo, onReopen, onPick });
        tone(toneFor(r));
        const picking = r.candidates && r.candidates.length;
        later(picking ? PICK_MS : (r.can_undo || r.can_reopen) ? ACTION_MS : RESULT_MS);
    }
    function showQueued() {
        TapScreen.message(screen, 'amber', 'SAVED', '',
            'No connection. This tap is saved on the station and will send when it is back online.');
        tone('queued');
        updateQueueChip();
        later(3500);
    }
    function showError(text) {
        TapScreen.message(screen, 'red', '!', '', text);
        tone('err');
        later(5000);
    }
    function showSignedOut() {
        setNet('signin');
        TapScreen.message(screen, 'red', '!', 'Sign in again',
            'Taps are being saved on this station. Sign in and they will send.',
            [TapScreen.link('Sign in', '/auth/login?next=' + encodeURIComponent('/tap-in/station'))]);
        tone('err');
        clearTimeout(resetTimer);
    }

    // ── Network ──────────────────────────────────────────────────────
    async function call(url, body, ms) {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), ms || 8000);
        try {
            const res = await fetch(url, {
                method: body === undefined ? 'GET' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: body === undefined ? undefined : JSON.stringify(body),
                signal: ctrl.signal, cache: 'no-store', credentials: 'same-origin',
            });
            let data = {};
            try { data = await res.json(); } catch (e) { data = {}; }
            return { res, data };
        } finally { clearTimeout(t); }
    }

    async function submit(t) {
        const id = t.token || t.uid;
        if (!id) return;
        const now = Date.now();
        if ((lastSeen.get(id) || 0) > now - LOCAL_REPEAT_MS) return;
        lastSeen.set(id, now);
        const item = { event_key: TapNFC.newEventKey('kiosk'), token: t.token || null,
                       uid: t.uid || null, source: t.source, occurred_at: now };
        // Keep order: while anything is queued, new taps join the queue.
        if (signedOut || (await qCount()) > 0) {
            await qAdd(item);
            if (signedOut) { showSignedOut(); updateQueueChip(); return; }
            showQueued();
            flush();
            return;
        }
        TapScreen.message(screen, 'idle', 'spinner', '', '');
        let out;
        try {
            out = await call('/api/tap', { event_key: item.event_key, token: item.token,
                                           uid: item.uid, source: item.source });
        } catch (e) {
            setNet('offline');
            await qAdd(item);
            showQueued();
            return;
        }
        if (out.res.status === 401) { signedOut = true; await qAdd(item); showSignedOut(); updateQueueChip(); return; }
        setNet('online');
        if (!out.res.ok) { showError(out.data.error || ('Error ' + out.res.status)); return; }
        showResult(out.data);
    }

    async function flush() {
        if (flushing) return;
        flushing = true;
        try {
            // One pass over what is queued now. A tap is removed only when the
            // server recorded it, or settled it for good (too old to record:
            // the server logs those for staff). Anything retryable stays.
            const items = await qAll();
            let refused = 0;
            for (let i = 0; i < items.length; i += 50) {
                const chunk = items.slice(i, i + 50);
                let out;
                try { out = await call('/api/tap/batch', { taps: chunk }, 20000); }
                catch (e) { setNet('offline'); return; }
                if (out.res.status === 401) { signedOut = true; showSignedOut(); return; }
                if (!out.res.ok) { setNet('online'); return; }
                setNet('online');
                const results = out.data.results || [];
                const settled = results.filter(x => x.event_key && (x.ok || x.retry === false));
                refused += settled.filter(x => !x.ok).length;
                if (settled.length) await qDel(settled.map(x => x.event_key));
                if (settled.length < chunk.length) break;  // keep the rest; try again later
            }
            if (refused) showError(refused + " saved tap(s) were too old to record. They're listed under Today's taps so staff can fix the card.");
        } finally {
            flushing = false;
            updateQueueChip();
        }
    }

    async function ping() {
        try {
            const out = await call('/api/tap/ping', undefined, 6000);
            if (out.res.status === 401) { signedOut = true; setNet('signin'); return false; }
            if (out.res.ok) {
                const wasOut = signedOut;
                signedOut = false;
                setNet('online');
                if (wasOut && current === null) ready();
                return true;
            }
        } catch (e) { setNet('offline'); }
        return false;
    }

    // ── Result-screen actions ────────────────────────────────────────
    async function onUndo(btn) {
        btn.disabled = true;
        try {
            const out = await call('/api/tap/' + current.event_id + '/undo', {});
            if (out.res.status === 401) { signedOut = true; return showSignedOut(); }
            if (!out.res.ok) { btn.disabled = false; return showError(out.data.error || 'Could not undo'); }
            TapScreen.message(screen, 'grey', 'UNDONE', current.student ? current.student.first_name : '',
                'Nothing is recorded for that tap.');
            later(3500);
        } catch (e) { btn.disabled = false; showError('No connection. Try Undo again.'); }
    }
    async function onReopen(btn) {
        btn.disabled = true;
        try {
            const out = await call('/api/tap/' + current.event_id + '/reopen', {});
            if (out.res.status === 401) { signedOut = true; return showSignedOut(); }
            if (!out.res.ok) { btn.disabled = false; return showError(out.data.error || 'Could not reopen'); }
            showResult(out.data);
        } catch (e) { btn.disabled = false; showError('No connection. Try again.'); }
    }
    async function onPick(cls, btn) {
        btn.disabled = true;
        try {
            const out = await call('/api/tap/' + current.event_id + '/pick',
                { event_key: TapNFC.newEventKey('pick'), class_id: cls.id });
            if (out.res.status === 401) { signedOut = true; return showSignedOut(); }
            if (!out.res.ok) { btn.disabled = false; return showError(out.data.error || 'Could not check in'); }
            showResult(out.data);
        } catch (e) { btn.disabled = false; showError('No connection. Pick the class again.'); }
    }

    // ── Inputs ───────────────────────────────────────────────────────
    async function startNfc() {
        if (!TapNFC.supported()) return;
        try {
            nfcAbort = new AbortController();
            const reader = new NDEFReader();
            await reader.scan({ signal: nfcAbort.signal });
            reader.onreading = (ev) => {
                const token = TapNFC.tokenFromUrl(TapNFC.firstUrl(ev.message));
                submit({ token: token || null, uid: token ? null : ev.serialNumber, source: 'kiosk' });
            };
            reader.onreadingerror = () => showError("Couldn't read that tag. Hold it flat and still.");
            nfcOn = true;
        } catch (e) {
            nfcOn = false;
            showError(TapNFC.errorText(e));
        }
        updateModeChip();
    }

    // USB keyboard-wedge reader: a burst of hex keys ending in Enter.
    let buf = '', lastKey = 0;
    document.addEventListener('keydown', (e) => {
        const tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA') return;
        const now = performance.now();
        if (now - lastKey > 100) buf = '';
        lastKey = now;
        if (e.key === 'Enter') {
            const s = buf.replace(/[^0-9a-fA-F]/g, '');
            buf = '';
            if (s.length === 8 || s.length === 14 || s.length === 20) {
                e.preventDefault();
                if (!usbSeen) { usbSeen = true; updateModeChip(); }
                submit({ token: null, uid: s, source: 'usb' });
            }
            return;
        }
        if (e.key && e.key.length === 1) buf += e.key;
    });

    async function keepAwake() {
        if (!('wakeLock' in navigator)) return;
        try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) { wakeLock = null; }
    }
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && started) { keepAwake(); ping().then(ok => { if (ok) flush(); }); }
    });

    async function start() {
        started = true;
        try {
            audio = new (window.AudioContext || window.webkitAudioContext)();
            await audio.resume();
        } catch (e) { audio = null; }
        await keepAwake();
        ready();
        await startNfc();
        updateModeChip();
        if (await ping()) flush();
    }

    window.addEventListener('online', () => { ping().then(ok => { if (ok) flush(); }); });
    window.addEventListener('offline', () => setNet('offline'));
    setInterval(async () => {
        if (!started) return;
        if ((await qCount()) > 0) { if (await ping()) flush(); }
    }, 10000);
    setInterval(() => { if (started) ping(); }, 60000);

    setNet(navigator.onLine ? 'online' : 'offline');
    updateModeChip();
    updateQueueChip();
    startScreen();
    ping();
})();
