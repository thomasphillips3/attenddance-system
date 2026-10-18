"""Teacher/admin manual access and portal navigation. Uses an in-memory DB.

Run: RFID_ENABLED=false venv/bin/python tests/test_manual.py
"""
import os
import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('RFID_ENABLED', 'false')

from app import create_app, db
from app.models import User


class ManualParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if tag == 'a' and 'href' in attrs:
            self.links.append(attrs['href'])


class ManualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            for role in ('teacher', 'parent'):
                user = User(username=role, email=f'{role}@example.test',
                            first_name='Test', last_name=role,
                            role=role, is_admin=False, is_active=True)
                user.set_password('manual-test-only')
                db.session.add(user)
            db.session.commit()
            cls.users = {u.username: str(u.id) for u in User.query.all()}

    def client_for(self, role):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = self.users[role]
            session['_fresh'] = True
        return client

    def test_staff_can_read_manual_and_follow_portal_links(self):
        for role in ('admin', 'teacher'):
            with self.subTest(role=role):
                client = self.client_for(role)
                dashboard = client.get('/dashboard')
                self.assertEqual(dashboard.status_code, 200)
                portal = ManualParser()
                portal.feed(dashboard.get_data(as_text=True))
                # Sidebar and dashboard both provide a real route to the guide.
                self.assertGreaterEqual(portal.links.count('/manual'), 2)
                response = client.get('/manual')
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('<title>User Manual - LSO Dance</title>', html)
                self.assertIn('Teacher daily checklist', html)
                self.assertIn('Admin setup checklist', html)
                self.assertIn('October 17, 2026', html)

    def test_anonymous_and_parent_cannot_read_staff_manual(self):
        response = self.app.test_client().get('/manual')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/auth/login', response.location)
        parent = self.client_for('parent')
        response = parent.get('/manual')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/parent'))
        self.assertNotIn('href="/manual"', parent.get('/parent').get_data(as_text=True))

    def test_manual_explains_how_to_tap_a_tag(self):
        html = self.client_for('teacher').get('/manual').get_data(as_text=True)
        self.assertIn('id="tap-how"', html)
        self.assertIn('href="#tap-how"', html)
        for phrase in ('flat against the back', 'top edge', 'NOT RECORDED',
                       'What to tell families', 'NTAG215',
                       'may or may not have saved', 'USB-only'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, html)
        # NOT RECORDED is uncertain, never "nothing saved".
        self.assertNotIn('so nothing saved', html)
        self.assertNotIn('attendance has not saved', html)
        # House style: no em or en dashes in manual copy.
        self.assertNotIn('\u2014', html.split('id="tap-how"')[1].split('Tap-In setup, tag status')[0])

    def test_teacher_manual_access_does_not_grant_admin_controls(self):
        client = self.client_for('teacher')
        for path in ('/settings', '/transactions', '/staff', '/pending-payments'):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 302)
        for path in ('/api/transactions', '/api/settings/payments', '/api/staff'):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 403)

    def test_contents_links_have_unique_destinations(self):
        html = self.client_for('teacher').get('/manual').get_data(as_text=True)
        parsed = ManualParser()
        parsed.feed(html)
        self.assertEqual(len(parsed.ids), len(set(parsed.ids)))
        for link in parsed.links:
            if link.startswith('#') and len(link) > 1:
                with self.subTest(link=link):
                    self.assertIn(link[1:], parsed.ids)
        self.assertEqual(len(re.findall(r'<h2 id="', html)), 15)


if __name__ == '__main__':
    unittest.main(verbosity=2)
