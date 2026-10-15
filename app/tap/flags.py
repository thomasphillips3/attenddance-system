"""Staff-only badges on the tap result screen.

The screen faces the line, so a flag is a neutral word and never a dollar
amount, and nothing here ever blocks a check-in.

"Check account" uses the household's NET balance per fund, the same rule as
balance reminders (helpers.family_reminder_groups): a credit on one sibling
cancels a charge on another, but a studio credit never hides a Company debt.
The account badge is admin-only, like every other billing view.
"Forms needed" means an active waiver template with no signature row for this
dancer. WaiverTemplate has no "required" flag, so every active form counts.
"""

from app import db
from app.funds import FUNDS
from app.helpers import calc_balance_bulk
from app.models import Student, WaiverSignature, WaiverTemplate

OWED_EPSILON = 0.005


def household_owes(student) -> bool:
    if student.family_id:
        members = Student.query.filter_by(family_id=student.family_id, is_active=True).all()
        if student not in members:
            members.append(student)
    else:
        members = [student]
    balances = calc_balance_bulk([s.id for s in members])
    for fund in FUNDS:
        net = sum(balances[s.id][fund]['balance'] for s in members)
        if net > OWED_EPSILON:
            return True
    return False


def forms_needed(student) -> bool:
    active = {tid for (tid,) in db.session.query(WaiverTemplate.id).filter(
        WaiverTemplate.is_active.is_(True))}
    if not active:
        return False
    signed = {tid for (tid,) in db.session.query(WaiverSignature.template_id).filter(
        WaiverSignature.student_id == student.id)}
    return bool(active - signed)


def flags_for(student, include_money=False) -> list[dict]:
    """Badges for one dancer. `include_money` is the caller's admin check:
    teachers take attendance but don't see billing (least privilege, Thomas
    2026-07-06), so they never get the account badge."""
    if student is None:
        return []
    out = []
    if include_money and household_owes(student):
        out.append({'code': 'check_account', 'label': 'Check account'})
    if forms_needed(student):
        out.append({'code': 'forms_needed', 'label': 'Forms needed'})
    return out
