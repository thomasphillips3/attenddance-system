/**
 * Fall Go-Live Checklist — Apps Script backend.
 * Paste this into Extensions → Apps Script on the checklist Google Sheet,
 * then Deploy → New deployment → Web app → Execute as: Me →
 * Who has access: Anyone → Deploy. Copy the resulting /exec URL into
 * index.html's API_BASE constant.
 *
 * Sheet must have a tab named "Checklist" with header row:
 * item_id | sort_order | title | note | done | done_at | updated_at
 */

const SHEET_NAME = 'Checklist';

// Canonical seed TSV, published to S3 by deploy.sh. syncContent() reads item
// copy from here so sheet-seed-data.tsv stays the single source of truth.
const SEED_URL = 'https://attenddance-checklist.s3.amazonaws.com/seed.tsv';

function _sheet() {
  return SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
}

// Normalize an item_id for matching. Google Sheets silently coerces a
// leading-zero string like "01" to the number 1 on paste, so the seed's "01"
// and the sheet's 1 must compare equal. Strip leading zeros on both sides.
function _normId(v) {
  return String(v).trim().replace(/^0+(?=\d)/, '');
}

/**
 * Maintenance — run from the Apps Script editor after editing
 * sheet-seed-data.tsv and re-running ./deploy.sh. Rebuilds the whole data
 * region from the published seed TSV: one row per seed item, in seed order,
 * with the canonical title/note/sort_order. Preserves done / done_at per
 * item (matched by normalized id, so check-offs survive), and is fully
 * idempotent + self-healing — it collapses any duplicate rows and drops rows
 * whose item_id is no longer in the seed. Safe to run repeatedly.
 */
function syncContent() {
  const tsv = UrlFetchApp.fetch(SEED_URL, { muteHttpExceptions: true }).getContentText();
  const lines = tsv.split(/\r?\n/).filter(l => l.trim() !== '');
  const sh = lines[0].split('\t');
  const sId = sh.indexOf('item_id');
  const sSort = sh.indexOf('sort_order');
  const sTitle = sh.indexOf('title');
  const sNote = sh.indexOf('note');
  const sDetails = sh.indexOf('details');

  const seed = lines.slice(1).map(line => {
    const c = line.split('\t');
    return {
      id: c[sId],
      sort_order: c[sSort],
      title: c[sTitle],
      note: c[sNote],
      // Optional column; rows are allowed to be short, so guard the index.
      details: sDetails >= 0 ? (c[sDetails] || '') : '',
    };
  });

  const sheet = _sheet();
  const range = sheet.getDataRange();
  const rows = range.getValues();
  const h = rows[0];

  // Self-healing schema: when the seed introduces a column the sheet predates
  // (e.g. `details`), append it to the header instead of silently dropping the
  // data — h.indexOf() would return -1 and write to a bogus array index.
  ['item_id', 'sort_order', 'title', 'note', 'details', 'done', 'done_at', 'updated_at']
    .forEach(name => { if (h.indexOf(name) === -1) h.push(name); });

  const width = h.length;
  const idCol = h.indexOf('item_id');
  const sortCol = h.indexOf('sort_order');
  const titleCol = h.indexOf('title');
  const noteCol = h.indexOf('note');
  const detailsCol = h.indexOf('details');
  const doneCol = h.indexOf('done');
  const doneAtCol = h.indexOf('done_at');
  const updCol = h.indexOf('updated_at');

  // Capture existing done-state per normalized id (a TRUE anywhere wins, so a
  // duplicate pair doesn't lose a check-off).
  const state = {};
  for (let r = 1; r < rows.length; r++) {
    const id = _normId(rows[r][idCol]);
    if (!id) continue;
    const done = rows[r][doneCol] === true ||
                 String(rows[r][doneCol]).toUpperCase() === 'TRUE';
    if (!state[id] || (done && !state[id].done)) {
      state[id] = {
        done: done,
        done_at: done ? rows[r][doneAtCol] : '',
        updated_at: rows[r][updCol] || '',
      };
    }
  }

  // Rebuild the data block fresh, header + one row per seed item.
  const out = [h];
  seed.forEach(item => {
    const s = state[_normId(item.id)] || { done: false, done_at: '', updated_at: '' };
    const row = new Array(width).fill('');
    row[idCol] = item.id;
    row[sortCol] = item.sort_order;
    row[titleCol] = item.title;
    row[noteCol] = item.note;
    row[detailsCol] = item.details;
    row[doneCol] = s.done;
    row[doneAtCol] = s.done_at || '';
    row[updCol] = s.updated_at || '';
    out.push(row);
  });

  range.clearContent();
  sheet.getRange(1, 1, out.length, width).setValues(out);
}

function _cors(output) {
  // Pass-through seam. Apps Script Web Apps don't handle OPTIONS preflight, so
  // the frontend deliberately avoids sending requests that would trigger one
  // (no custom Content-Type header), which means no CORS headers are needed
  // today. This wrapper returns the output unchanged — it's just a single
  // choke point to add Access-Control-Allow-Origin later if a frontend change
  // ever needs it. (ContentService can't set arbitrary response headers, so
  // any real CORS support would require switching to a different response type.)
  return output;
}

function doGet(e) {
  const sheet = _sheet();
  const rows = sheet.getDataRange().getValues();
  const headers = rows[0];
  const items = rows.slice(1)
    .filter(row => row.some(cell => cell !== ''))
    .map(row => {
      const obj = {};
      headers.forEach((h, i) => { obj[h] = row[i]; });
      return obj;
    })
    .sort((a, b) => a.sort_order - b.sort_order);

  return _cors(ContentService.createTextOutput(JSON.stringify({ items }))
    .setMimeType(ContentService.MimeType.JSON));
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const body = JSON.parse(e.postData.contents);
    const itemId = _normId(body.item_id || '');
    if (!itemId) {
      return _cors(ContentService.createTextOutput(JSON.stringify({ error: 'item_id is required' }))
        .setMimeType(ContentService.MimeType.JSON));
    }
    const done = !!body.done;
    const now = new Date().toISOString();

    const sheet = _sheet();
    const rows = sheet.getDataRange().getValues();
    const headers = rows[0];
    const idCol = headers.indexOf('item_id');
    const doneCol = headers.indexOf('done');
    const doneAtCol = headers.indexOf('done_at');
    const updatedAtCol = headers.indexOf('updated_at');

    // Match with _normId, not raw String(): Sheets coerces a leading-zero id
    // like "01" to the number 1, so the frontend's "01" and the cell's 1 must
    // compare equal (same rule syncContent uses). And update EVERY row that
    // shares the id rather than break-on-first — if a duplicate pair still
    // exists, updating only one lets its TRUE twin outvote an un-check on the
    // next read, which is exactly "can't check items off".
    let matched = false;
    for (let r = 1; r < rows.length; r++) {
      if (_normId(rows[r][idCol]) === itemId) {
        sheet.getRange(r + 1, doneCol + 1).setValue(done);
        sheet.getRange(r + 1, doneAtCol + 1).setValue(done ? now : '');
        sheet.getRange(r + 1, updatedAtCol + 1).setValue(now);
        matched = true;
      }
    }
    return _cors(ContentService.createTextOutput(JSON.stringify({ ok: matched }))
      .setMimeType(ContentService.MimeType.JSON));
  } finally {
    lock.releaseLock();
  }
}
