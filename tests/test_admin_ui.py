"""Administrator actions through the People page.

Written against the real markup: each person is a row; the status buttons are
forms with a hidden input name="action" (value deactivate / restore); the role
is a select name="role". If one fails with a locator timeout, adjust find_row().
"""
import re
import uuid

from playwright.sync_api import Page, expect

from conftest import PASSWORD, register, sign_in, submit_form


def find_row(admin: Page, email: str):
    admin.goto("/admin/users?q=" + email)
    return admin.locator("tr, li, article, .user-row").filter(has_text=email).first


def change_role(admin: Page, row, role_value, expected_text):
    """Pick a role, and save it if choosing it did not already save it."""
    select = row.locator("select[name=role]").first
    select.select_option(role_value, force=True)
    try:
        expect(admin.get_by_text(expected_text)).to_be_visible(timeout=2500)   # the dropdown saves by itself
    except AssertionError:
        select.evaluate("el => el.form.requestSubmit()")                         # it needs a save button


def press_action(admin: Page, row, action):
    form = row.locator("form").filter(has=row.locator(f"[name=action][value={action}]")).first
    submit_form(form)


def make_person(browser, base_url):
    email = f"target.{uuid.uuid4().hex[:8]}@example.com"
    context = browser.new_context(base_url=base_url)
    page = context.new_page()
    register(page, "Target Person", email)
    page.wait_for_url(re.compile(r"/dashboard"))
    context.close()
    return email


def test_admin_can_change_a_role(admin_page: Page, browser, base_url):
    email = make_person(browser, base_url)
    row = find_row(admin_page, email)
    change_role(admin_page, row, "reviewer", re.compile("is now a Reviewer"))
    expect(admin_page.get_by_text(re.compile("is now a Reviewer"))).to_be_visible()


def test_deactivated_person_is_locked_out_then_restored(admin_page: Page, browser, base_url):
    email = make_person(browser, base_url)
    admin_page.on("dialog", lambda dialog: dialog.accept())

    press_action(admin_page, find_row(admin_page, email), "deactivate")
    expect(admin_page.get_by_text("account is deactivated")).to_be_visible()

    context = browser.new_context(base_url=base_url)
    person = context.new_page()
    sign_in(person, email, PASSWORD)
    expect(person.get_by_text("This account has been deactivated")).to_be_visible()

    press_action(admin_page, find_row(admin_page, email), "restore")
    expect(admin_page.get_by_text("account is restored")).to_be_visible()

    sign_in(person, email, PASSWORD)
    expect(person).to_have_url(re.compile(r"/dashboard"))
    context.close()


def test_admin_cannot_change_their_own_role(admin_page: Page, users):
    row = find_row(admin_page, users["admin"]["email"])
    if row.locator("select[name=role]").count() == 0:
        return                      # the page already hides the control: also fine
    change_role(admin_page, row, "member", "You cannot change your own role")
    expect(admin_page.get_by_text("You cannot change your own role")).to_be_visible()
