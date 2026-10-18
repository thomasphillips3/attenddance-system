/* Web NFC helpers shared by tag setup and the tap station.
 *
 * Web NFC exists only in Chrome on Android (HTTPS, a user tap to start).
 * Serial numbers come back as "04:a2:1b:..."; the server normalizes them, so
 * they are sent as-is. A URL record's payload decodes with TextDecoder.
 */
(function () {
    const TapNFC = {};

    TapNFC.supported = function () {
        return 'NDEFReader' in window;
    };

    TapNFC.firstUrl = function (message) {
        if (!message || !message.records) return '';
        for (const rec of message.records) {
            if (rec.recordType === 'url' || rec.recordType === 'absolute-url') {
                try { return new TextDecoder().decode(rec.data); } catch (e) { return ''; }
            }
        }
        return '';
    };

    // Token from a tag URL ending in /t/<token>, or ''.
    TapNFC.tokenFromUrl = function (url) {
        const m = /\/t\/([A-Za-z0-9_-]{16,64})\/?$/.exec(url || '');
        return m ? m[1] : '';
    };

    // A friendly sentence for the DOMException names Web NFC throws.
    TapNFC.errorText = function (err) {
        const name = err && err.name;
        if (name === 'NotAllowedError') return 'NFC permission was blocked. Allow NFC for this site in Chrome settings.';
        if (name === 'NotSupportedError') return "This phone's NFC is off or missing. Turn NFC on in Settings.";
        if (name === 'NotReadableError') return "Couldn't talk to the tag. Hold it still on the back of the phone.";
        if (name === 'NetworkError') return 'The tag moved before the write finished. Hold it still and try again.';
        if (name === 'AbortError') return 'Stopped.';
        return (err && err.message) || 'NFC error';
    };

    TapNFC.newEventKey = function (prefix) {
        const rnd = (window.crypto && crypto.randomUUID) ? crypto.randomUUID().replace(/-/g, '')
            : Math.random().toString(36).slice(2) + Date.now().toString(36);
        return (prefix || 'tap') + '-' + rnd.slice(0, 40);
    };

    window.TapNFC = TapNFC;
})();
