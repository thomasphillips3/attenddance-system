"""Payments split across charges: allocations, partial amounts, edits.

A payment can name several charges and how much goes to each. Parts can be
less than the charge (partial payment) and can add up to less than the
payment (the rest pays the oldest charges). None of this changes a dancer's
total balance; it only decides which charges read as paid.
"""

import os
import sys
import tempfile
from datetime import date
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

db_file = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
db_file.close()
os.environ['DATABASE_URL'] = f'sqlite:///{db_file.name}'
os.environ['RFID_ENABLED'] = 'false'

import sqlalchemy  # noqa: E402

from app import create_app, db  # noqa: E402
from app.funds import fund_for_category  # noqa: E402
from app.helpers import build_aging, calc_balance  # noqa: E402
from app.migrations import _move_applied_charges_to_allocations  # noqa: E402
from app.models import AuditLog, PaymentAllocation, Setting, Student, Transaction, User  # noqa: E402

app = create_app('development')
app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)


def _charge(student_id, amount, category, fund, description, day=date(2026, 9, 1)):
    t = Transaction(student_id=student_id, type='charge', amount=amount, category=category,
                    fund=fund, description=description, transaction_date=day)
    db.session.add(t)
    return t


def _setup():
    with app.app_context():
        admin = User(username='linkadmin', email='link@example.test', first_name='Link',
                     last_name='Admin', role='admin', is_admin=True, is_active=True)
        admin.set_password('pw')
        student = Student(first_name='A', last_name='Student')
        other = Student(first_name='B', last_name='Student')
        db.session.add_all([admin, student, other])
        db.session.flush()
        ids = {
            'sid': student.id, 'other': other.id,
            'old': _charge(student.id, '100.00', 'tuition', 'studio', 'Old tuition', date(2026, 1, 1)),
            'new': _charge(student.id, '60.00', 'tuition', 'studio', 'New tuition'),
            'costume': _charge(student.id, '200.00', 'costumes', 'studio', 'Recital costume'),
            'company': _charge(student.id, '40.00', 'competition', 'company', 'Competition'),
            'late': _charge(student.id, '15.00', 'late fee', 'studio', 'Late fee'),
            'foreign': _charge(other.id, '20.00', 'tuition', 'studio', 'Other kid'),
            'race': _charge(other.id, '100.00', 'tuition', 'studio', 'Concurrent charge'),
        }
        db.session.commit()
        return {k: (v if isinstance(v, int) else v.id) for k, v in ids.items()}


def _login(client):
    client.post('/auth/login', data={'username': 'linkadmin', 'password': 'pw'})


def test_allocations_and_edits():
    ids = _setup()
    sid = ids['sid']
    url = f'/api/students/{sid}/open-charges'

    def owed():
        return {c['id']: c['remaining'] for c in client.get(url).get_json()['charges']}

    with app.test_client() as client:
        _login(client)
        assert set(owed()) == {ids['old'], ids['new'], ids['costume'], ids['company'], ids['late']}

        base = {'student_id': sid, 'type': 'payment', 'category': 'tuition',
                'payment_method': 'cash', 'transaction_date': '2026-10-01'}

        # Partial: $100 toward the $200 costume (Carollette's case). The
        # payment's category doesn't have to match the charge.
        r = client.post('/api/transactions', json={**base, 'amount': '100.00',
                        'allocations': [{'charge_id': ids['costume'], 'amount': '100.00'}]})
        assert r.status_code == 201, r.get_json()
        partial = r.get_json()
        assert partial['allocations'][0]['charge_description'] == 'Recital costume'
        assert owed()[ids['costume']] == 100.0
        # The category breakdown follows the link: costumes shows it paid,
        # even though the payment itself is filed under tuition.
        cats = client.get(f'/api/students/{sid}/ledger').get_json()['by_category']
        assert cats['costumes']['payments'] == '100.00' and cats['tuition']['payments'] == '0.00', cats

        # One payment, three charges, with $5 left over paying the oldest charge.
        r = client.post('/api/transactions', json={**base, 'amount': '80.00', 'allocations': [
            {'charge_id': ids['new'], 'amount': '60.00'},
            {'charge_id': ids['late'], 'amount': '10.00'},
            {'charge_id': ids['costume'], 'amount': '5.00'},
        ]})
        assert r.status_code == 201, r.get_json()
        multi = r.get_json()
        assert len(multi['allocations']) == 3
        left = owed()
        assert ids['new'] not in left
        assert left[ids['late']] == 5.0 and left[ids['costume']] == 95.0
        assert left[ids['old']] == 95.0  # the $5 left over paid the oldest charge
        assert left[ids['company']] == 40.0  # never crosses funds

        with app.app_context():
            # Linking moves nothing on the balance: 375 studio charged, 180 paid.
            assert calc_balance(sid)['studio']['balance'] == 195.0
            aging = build_aging(Transaction.query.filter_by(student_id=sid, fund='studio').all(),
                                as_of=date(2026, 10, 17))
            assert aging['d90_plus'] == 95.0

        # Refusals: another kid's charge, the other fund, over the remainder,
        # parts over the payment, a duplicate, a charge row, a bad list.
        bad = [
            {'allocations': [{'charge_id': ids['foreign'], 'amount': '5'}]},
            {'fund': 'studio', 'allocations': [{'charge_id': ids['company'], 'amount': '5'}]},
            {'allocations': [{'charge_id': ids['costume'], 'amount': '95.01'}]},
            {'amount': '10', 'allocations': [{'charge_id': ids['old'], 'amount': '11'}]},
            {'allocations': [{'charge_id': ids['old'], 'amount': '1'}, {'charge_id': ids['old'], 'amount': '1'}]},
            {'allocations': [{'charge_id': ids['old'], 'amount': '0'}]},
            {'allocations': 'nope'},
            {'type': 'charge', 'allocations': [{'charge_id': ids['old'], 'amount': '1'}]},
        ]
        for changed in bad:
            r = client.post('/api/transactions', json={**base, 'amount': '50.00', **changed})
            assert r.status_code == 400, (changed, r.get_json())

        # A Company payment picks up its fund from the charge it pays.
        r = client.post('/api/transactions', json={**base, 'amount': '40.00', 'category': 'competition',
                        'allocations': [{'charge_id': ids['company'], 'amount': '40.00'}]})
        assert r.status_code == 201 and r.get_json()['fund'] == 'company'
        assert ids['company'] not in owed()

        # The older single-charge shape still works and pays the whole amount.
        r = client.post('/api/transactions', json={**base, 'amount': '5.00', 'applied_charge_id': ids['late']})
        assert r.status_code == 201 and r.get_json()['allocations'][0]['charge_id'] == ids['late']
        assert ids['late'] not in owed()

        # Editing a payment: the form lists what each charge owed before it.
        before = {c['id']: c['remaining'] for c in
                  client.get(url + f'?exclude_payment={partial["id"]}').get_json()['charges']}
        assert before[ids['costume']] == 195.0

        # Fix the amount and the split in place (no delete + re-enter).
        r = client.put(f'/api/transactions/{partial["id"]}', json={
            'amount': '120.00', 'payment_method': 'zelle', 'description': 'Fixed',
            'allocations': [{'charge_id': ids['costume'], 'amount': '120.00'}]})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['payment_method'] == 'zelle' and r.get_json()['amount'] == '120.00'
        assert owed()[ids['costume']] == 75.0
        # Leaving allocations out keeps the existing split.
        r = client.put(f'/api/transactions/{partial["id"]}', json={'transaction_date': '2026-10-02'})
        assert r.status_code == 200 and len(r.get_json()['allocations']) == 1
        # Lowering the amount under the split is refused; so is changing type or student.
        assert client.put(f'/api/transactions/{partial["id"]}', json={'amount': '50'}).status_code == 400
        assert client.put(f'/api/transactions/{partial["id"]}', json={'type': 'charge'}).status_code == 400
        assert client.put(f'/api/transactions/{partial["id"]}',
                          json={'student_id': ids['other']}).status_code == 400
        # Unlinking everything turns it back into an oldest-first payment.
        r = client.put(f'/api/transactions/{partial["id"]}', json={'allocations': []})
        assert r.status_code == 200 and r.get_json()['allocations'] == []

        # Editing a charge: its amount can't drop below what's linked to it,
        # and it can't move funds while payments are linked.
        assert client.put(f'/api/transactions/{ids["new"]}', json={'amount': '59.99'}).status_code == 400
        assert client.put(f'/api/transactions/{ids["new"]}',
                          json={'category': 'competition'}).status_code == 400
        r = client.put(f'/api/transactions/{ids["old"]}', json={'amount': '90.00', 'description': 'Jan tuition'})
        assert r.status_code == 200 and r.get_json()['description'] == 'Jan tuition'
        with app.app_context():
            assert AuditLog.query.filter_by(action='transaction.update').count() >= 4

        # The ledger tells each charge row whether it's paid.
        ledger = client.get(f'/api/students/{sid}/ledger').get_json()['ledger']
        status = {r['id']: r.get('remaining') for r in ledger if r['type'] == 'charge'}
        assert status[ids['new']] == '0.00' and status[ids['company']] == '0.00'
        listing = client.get(f'/api/transactions?student_id={sid}').get_json()['transactions']
        assert {t['id']: t.get('remaining') for t in listing if t['type'] == 'charge'}[ids['new']] == '0.00'

        # Deleting a charge frees the payments split onto it.
        assert client.delete(f'/api/transactions/{ids["new"]}').status_code == 200
        with app.app_context():
            assert PaymentAllocation.query.filter_by(charge_id=ids['new']).count() == 0
            assert len(db.session.get(Transaction, multi['id']).allocations) == 2
        # Deleting a payment takes its own allocations with it.
        assert client.delete(f'/api/transactions/{multi["id"]}').status_code == 200
        with app.app_context():
            assert PaymentAllocation.query.filter_by(payment_id=multi['id']).count() == 0

    # Two requests racing for the same remainder: only one wins.
    race_body = {'student_id': ids['other'], 'type': 'payment', 'category': 'tuition',
                 'payment_method': 'cash', 'amount': '60.00',
                 'allocations': [{'charge_id': ids['race'], 'amount': '60.00'}]}

    def post_racing_payment():
        with app.test_client() as c:
            _login(c)
            return c.post('/api/transactions', json=race_body).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: post_racing_payment(), range(2)))
    assert sorted(statuses) == [201, 400], statuses
    with app.app_context():
        assert PaymentAllocation.query.filter_by(charge_id=ids['race']).count() == 1

    # Both payment screens render the checkbox picker.
    with app.test_client() as client:
        _login(client)
        assert b'txn-charge-list' in client.get('/transactions').data
        assert b'lp-charge-list' in client.get(f'/students/{sid}/ledger').data
        assert client.get('/static/js/charge-picker.js').status_code == 200


def test_legacy_links_migrate():
    """Links made before allocations (transactions.applied_charge_id) become
    one allocation each, and the old column is cleared so it runs once."""
    with app.app_context():
        conn = db.session.connection()
        if 'applied_charge_id' not in {c['name'] for c in sqlalchemy.inspect(conn).get_columns('transactions')}:
            conn.execute(sqlalchemy.text('ALTER TABLE transactions ADD COLUMN applied_charge_id INTEGER'))
        s = Student(first_name='Legacy', last_name='Link')
        db.session.add(s)
        db.session.flush()
        charge = _charge(s.id, '50.00', 'tuition', 'studio', 'Legacy charge')
        pay = Transaction(student_id=s.id, type='payment', amount='30.00', category='tuition',
                          fund='studio', payment_method='cash')
        db.session.add(pay)
        db.session.flush()
        conn.execute(sqlalchemy.text('UPDATE transactions SET applied_charge_id = :c WHERE id = :p'),
                     {'c': charge.id, 'p': pay.id})
        db.session.commit()
        pay_id, charge_id = pay.id, charge.id

        for _ in range(2):
            with db.engine.connect() as raw:
                _move_applied_charges_to_allocations(raw, sqlalchemy.inspect(db.engine))
                raw.commit()
        rows = PaymentAllocation.query.filter_by(payment_id=pay_id).all()
        assert [(r.charge_id, float(r.amount)) for r in rows] == [(charge_id, 30.0)]
        left = db.session.execute(sqlalchemy.text(
            'SELECT COUNT(*) FROM transactions WHERE applied_charge_id IS NOT NULL')).scalar()
        assert left == 0


def test_company_other_and_paypal():
    assert fund_for_category('company_other') == 'company'
    assert fund_for_category('other') == 'studio'
    with app.app_context():
        Setting.set('company_payments_paypal_enabled', '1')
        Setting.set('company_payments_paypal_username', 'LSO Dance<script>')
        db.session.commit()
    with app.test_client() as client:
        _login(client)
        assert b'value="company_other"' in client.get('/transactions').data
        options = client.get('/api/payment-options').get_json()['payment_options']
        paypal = [o for o in options['company'] if o['type'] == 'paypal']
        assert paypal == [{'type': 'paypal', 'username': 'LSODancescript',
                           'url': 'https://paypal.me/LSODancescript'}], paypal
        assert not [o for o in options['studio'] if o['type'] == 'paypal']
        assert b'paypal-name' in client.get('/settings').data


def test_review_regressions():
    """Holes the adversarial review found, each pinned."""
    from app.models import ClassEnrollment, DanceClass, Family, RecurringCharge  # noqa: F401
    with app.app_context():
        fam = Family(name='Review family')
        db.session.add(fam)
        db.session.flush()
        a = Student(first_name='Sib', last_name='A', family_id=fam.id)
        b = Student(first_name='Sib', last_name='B', family_id=fam.id)
        c = Student(first_name='Solo', last_name='C')
        db.session.add_all([a, b, c])
        db.session.flush()
        b_charge = _charge(b.id, '100.00', 'tuition', 'studio', 'B tuition')
        db.session.add(Transaction(student_id=a.id, type='payment', amount='100.00', category='tuition',
                                   fund='studio', payment_method='cash', transaction_date=date(2026, 9, 2)))
        # C: an old unlinked payment already paying a charge oldest-first.
        c_charge = _charge(c.id, '80.00', 'tuition', 'studio', 'C tuition', date(2026, 8, 1))
        c_pay = Transaction(student_id=c.id, type='payment', amount='80.00', category='tuition',
                            fund='studio', payment_method='zelle', transaction_date=date(2026, 8, 5))
        co_pay = Transaction(student_id=c.id, type='payment', amount='25.00', category='tuition',
                             fund='company', payment_method='cash', transaction_date=date(2026, 8, 6))
        dues = _charge(c.id, '30.00', 'company_dues', 'company', 'Dues')
        rc_charge = _charge(c.id, '50.00', 'tuition', 'studio', 'Auto tuition', date(2026, 10, 1))
        db.session.add_all([c_pay, co_pay])
        db.session.flush()
        rc_charge.recurring_charge_id = 999
        db.session.commit()
        ids = dict(fam=fam.id, b_charge=b_charge.id, c=c.id, c_charge=c_charge.id, c_pay=c_pay.id,
                   co_pay=co_pay.id, dues=dues.id, rc=rc_charge.id)

    with app.test_client() as client:
        _login(client)
        # 1. A sibling's unlinked payment must not mark B's charge paid.
        rows = client.get(f"/api/families/{ids['fam']}/ledger").get_json()['ledger']
        assert {r['id']: r.get('remaining') for r in rows}[ids['b_charge']] == '100.00', rows

        # Editing an old unlinked payment to link the charge it already pays.
        listed = client.get(f"/api/students/{ids['c']}/open-charges?exclude_payment={ids['c_pay']}").get_json()['charges']
        assert ids['c_charge'] in {x['id'] for x in listed}
        r = client.put(f"/api/transactions/{ids['c_pay']}",
                       json={'allocations': [{'charge_id': ids['c_charge'], 'amount': '80.00'}]})
        assert r.status_code == 200, r.get_json()
        # ...and editing it again keeps that link listed even though it's paid.
        listed = client.get(f"/api/students/{ids['c']}/open-charges?exclude_payment={ids['c_pay']}").get_json()['charges']
        assert ids['c_charge'] in {x['id'] for x in listed}
        r = client.put(f"/api/transactions/{ids['c_pay']}", json={'description': 'still linked'})
        assert r.status_code == 200 and len(r.get_json()['allocations']) == 1

        # 2. A changed category must be real; transfer can't be picked.
        for bad in ('Company Dues', 'typo', 'transfer'):
            assert client.put(f"/api/transactions/{ids['dues']}", json={'category': bad}).status_code == 400, bad
        r = client.put(f"/api/transactions/{ids['dues']}", json={'category': 'company_other'})
        assert r.status_code == 200 and r.get_json()['fund'] == 'company'

        # 4. A null fund keeps a Company payment in the Company fund.
        r = client.put(f"/api/transactions/{ids['co_pay']}", json={'fund': None, 'amount': '20.00'})
        assert r.status_code == 200 and r.get_json()['fund'] == 'company', r.get_json()

        # 5. An auto-billed charge can't leave its month.
        assert client.put(f"/api/transactions/{ids['rc']}",
                          json={'transaction_date': '2026-09-30'}).status_code == 400
        assert client.put(f"/api/transactions/{ids['rc']}",
                          json={'transaction_date': '2026-10-20'}).status_code == 200


if __name__ == '__main__':
    try:
        test_allocations_and_edits()
        test_legacy_links_migrate()
        test_company_other_and_paypal()
        test_review_regressions()
        print('Applied payment tests passed')
    finally:
        os.unlink(db_file.name)
