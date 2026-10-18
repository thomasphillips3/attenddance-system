"""Payments can target a charge without changing the student's total balance."""

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

from app import create_app, db  # noqa: E402
from app.helpers import build_aging, calc_balance  # noqa: E402
from app.models import Student, Transaction, User  # noqa: E402

app = create_app('development')
app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)


def test_linked_payment_flow():
    with app.app_context():
        admin = User(username='linkadmin', email='link@example.test', first_name='Link',
                     last_name='Admin', role='admin', is_admin=True, is_active=True)
        admin.set_password('pw')
        student = Student(first_name='A', last_name='Student')
        other = Student(first_name='B', last_name='Student')
        db.session.add_all([admin, student, other])
        db.session.flush()
        old = Transaction(student_id=student.id, type='charge', amount='100.00',
                          category='tuition', fund='studio', description='Old tuition',
                          transaction_date=date(2026, 1, 1))
        new = Transaction(student_id=student.id, type='charge', amount='60.00',
                          category='tuition', fund='studio', description='New tuition',
                          transaction_date=date(2026, 9, 1))
        company = Transaction(student_id=student.id, type='charge', amount='40.00',
                              category='competition', fund='company', description='Competition')
        foreign = Transaction(student_id=other.id, type='charge', amount='20.00',
                              category='tuition', fund='studio')
        late_fee = Transaction(student_id=student.id, type='charge', amount='15.00',
                               category='late fee', fund='studio', description='Late fee')
        race = Transaction(student_id=other.id, type='charge', amount='100.00',
                           category='tuition', fund='studio', description='Concurrent charge')
        db.session.add_all([old, new, company, foreign, late_fee, race])
        db.session.commit()
        sid, other_id, old_id, new_id, company_id, foreign_id, late_id, race_id = (
            student.id, other.id, old.id, new.id, company.id, foreign.id, late_fee.id, race.id)

    with app.test_client() as client:
        client.post('/auth/login', data={'username': 'linkadmin', 'password': 'pw'})
        url = f'/api/students/{sid}/open-charges'
        charges = client.get(url).get_json()['charges']
        assert {c['id'] for c in charges} == {old_id, new_id, company_id, late_id}

        body = {'student_id': sid, 'type': 'payment', 'amount': '30.00',
                'category': 'tuition', 'payment_method': 'cash',
                'transaction_date': '2026-10-01', 'applied_charge_id': new_id}
        response = client.post('/api/transactions', json=body)
        assert response.status_code == 201, response.get_json()
        payment = response.get_json()
        assert payment['applied_charge_id'] == new_id
        assert payment['applied_charge_description'] == 'New tuition'
        assert client.get(url).get_json()['charges'][1]['remaining'] == 30.0

        with app.app_context():
            assert calc_balance(sid)['studio']['balance'] == 145.0
            aging = build_aging(Transaction.query.filter_by(student_id=sid, fund='studio').all(),
                                as_of=date(2026, 10, 17))
            assert aging['d90_plus'] == 100.0
            assert aging['d31_60'] == 30.0

        for changed in (
            {'applied_charge_id': foreign_id},
            {'applied_charge_id': company_id},
            {'applied_charge_id': old_id, 'amount': '101.00'},
            {'type': 'charge'},
        ):
            assert client.post('/api/transactions', json={**body, **changed}).status_code == 400

        second = client.post('/api/transactions', json={**body, 'amount': '30.00'})
        assert second.status_code == 201
        assert new_id not in {c['id'] for c in client.get(url).get_json()['charges']}
        assert client.post('/api/transactions', json=body).status_code == 400

        # An unlinked payment keeps paying the oldest charge, within its fund.
        unlinked = client.post('/api/transactions', json={**body, 'amount': '20.00',
                                                            'applied_charge_id': None})
        assert unlinked.status_code == 201
        remaining = {c['id']: c['remaining'] for c in client.get(url).get_json()['charges']}
        assert remaining[old_id] == 80.0 and remaining[company_id] == 40.0

        late_payment = client.post('/api/transactions', json={**body, 'amount': '15.00',
                               'category': 'late fee', 'applied_charge_id': late_id})
        assert late_payment.status_code == 201
        assert late_id not in {c['id'] for c in client.get(url).get_json()['charges']}

        # Removing a charge clears its payment references and leaves no dangling link.
        assert client.delete(f'/api/transactions/{new_id}').status_code == 200
        with app.app_context():
            assert db.session.get(Transaction, payment['id']).applied_charge_id is None

    race_body = {**body, 'student_id': other_id, 'applied_charge_id': race_id,
                 'amount': '60.00'}

    def post_racing_payment():
        with app.test_client() as client:
            client.post('/auth/login', data={'username': 'linkadmin', 'password': 'pw'})
            return client.post('/api/transactions', json=race_body).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: post_racing_payment(), range(2)))
    assert sorted(statuses) == [201, 400], statuses
    with app.app_context():
        assert Transaction.query.filter_by(applied_charge_id=race_id).count() == 1

    # Both payment entry screens expose a charge selector.
    with app.test_client() as client:
        client.post('/auth/login', data={'username': 'linkadmin', 'password': 'pw'})
        assert b'txn-applied-charge' in client.get('/transactions').data
        assert b'lp-charge' in client.get(f'/students/{sid}/ledger').data


if __name__ == '__main__':
    try:
        test_linked_payment_flow()
        print('Applied payment tests passed')
    finally:
        os.unlink(db_file.name)
